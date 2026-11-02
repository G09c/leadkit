"""Destino do webhook: para onde vão os eventos de cada formulário (ADR 0009, emenda de 2026-10-08).

- Um destino padrão, opcional, e no máximo um destino por formulário, pelo slug. Na hora do envio,
  a entrega usa o destino do formulário, senão o padrão; sem nenhum dos dois, não há entrega.
- O segredo de cada destino é gerado aqui e aparece uma vez, na criação e na troca. No banco, ele
  fica cifrado com WEBHOOK_ENCRYPTION_KEY, e nunca vai para log, auditoria ou tela depois disso.
- Toda mudança fica na auditoria em nome de quem a fez, sem o segredo e sem a URL inteira (o
  caminho de uma URL de webhook costuma valer como senha): só o host.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.adapters.webhook import Probe
from app.destination_purposes import (
    InvalidPurpose,
    require_internal,
    require_participant_capability,
    selected,
)
from app.models import AuditLog, WebhookDestination, WebhookEnvSeed, utcnow
from app.persisted_webhook import V2_SELECTION
from app.settings import Settings

if TYPE_CHECKING:
    from app.deps import Deps

logger = logging.getLogger(__name__)

Scope = Literal["form", "default"]
MAX_URL_LENGTH = 2000
TEST_EVENT = "webhook.teste"
ENV_ACTOR = "ambiente"  # autoria do destino padrão que veio do WEBHOOK_URL


class SecretCipher:
    """Cifra os segredos dos destinos com as chaves de WEBHOOK_ENCRYPTION_KEY: a primeira cifra,
    todas abrem (para trocar a chave sem perder os segredos já guardados)."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        self._fernets = [Fernet(key) for key in keys]
        self._all = MultiFernet(self._fernets)

    @classmethod
    def from_settings(cls, settings: Settings) -> SecretCipher | None:
        keys = settings.encryption_keys
        return cls(keys) if keys else None

    def seal(self, secret: str) -> str:
        return self._all.encrypt(secret.encode()).decode()

    def open(self, sealed: str) -> str:
        try:
            return self._all.decrypt(sealed.encode()).decode()
        except InvalidToken:
            raise UnreadableSecret from None

    def opens(self, sealed: str) -> bool:
        try:
            self._all.decrypt(sealed.encode())
        except InvalidToken:
            return False
        return True

    def reseal(self, sealed: str) -> str | None:
        """O segredo cifrado de novo com a primeira chave; None se ele já está nela."""
        try:
            self._fernets[0].decrypt(sealed.encode())
            return None
        except InvalidToken:
            pass
        try:
            return self._all.rotate(sealed.encode()).decode()
        except InvalidToken:
            raise UnreadableSecret from None


class UnreadableSecret(Exception):
    """O segredo guardado não abre: falta WEBHOOK_ENCRYPTION_KEY ou nenhuma das chaves serve."""


class NoCipher(Exception):
    """Sem WEBHOOK_ENCRYPTION_KEY não dá para guardar um segredo novo."""


class Conflict(Exception):
    """O destino mudou desde que a página foi aberta (outra aba, um clique repetido). `current` é
    como ele está agora (None se não existe), para a tela dizer quem o mudou."""

    def __init__(self, current: StoredDestination | None = None) -> None:
        super().__init__()
        self.current = current

    def by_the_same_click(self, actor: str, expected_version: int) -> bool:
        """A mudança que passou na frente é a do próprio `actor`, logo depois da versão que ele
        viu: o outro clique de um clique duplo (ou um F5 no POST)."""
        current = self.current
        return (
            current is not None
            and current.version == expected_version + 1
            and current.updated_by == actor
        )


class StartupError(RuntimeError):
    """A configuração do destino do webhook impede a subida; a mensagem diz o que fazer e nunca
    traz um valor do ambiente."""


class UnsupportedEvent(Exception):
    """O receptor não declarou aceitar o contrato solicitado no teste."""


class InvalidEvents(ValueError):
    """Os eventos declarados precisam ser corrigidos, independentemente da URL."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidUrl(ValueError):
    """A URL não serve para um destino. A mensagem é para a tela; `needs_confirmation` diz que
    falta só confirmar um endereço http em produção."""

    def __init__(self, message: str, *, needs_confirmation: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.needs_confirmation = needs_confirmation


def check_url(url: str, *, production: bool, http_confirmed: bool) -> str:
    """A URL do receptor, sem espaços nas pontas; senão `InvalidUrl` com o motivo. Só http ou
    https, com host, sem usuário e senha e com até MAX_URL_LENGTH caracteres. Em produção, http
    manda os dados das leads sem criptografia e pede `http_confirmed`."""
    url = url.strip()
    if not url:
        raise InvalidUrl("Informe a URL do receptor.")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidUrl(f"A URL passa de {MAX_URL_LENGTH} caracteres.")
    if any(char.isspace() or not char.isprintable() for char in url):
        raise InvalidUrl("A URL não pode ter espaços nem caracteres de controle.")
    try:
        parts = urlsplit(url)
    except ValueError:  # colchete sem par, ou um caractere que muda o host ao normalizar (NFKC)
        raise InvalidUrl("A URL não é válida.") from None
    if parts.scheme not in ("http", "https"):
        raise InvalidUrl("Use uma URL que comece com https:// (ou http://).")
    if "@" in parts.netloc:
        raise InvalidUrl(
            "Tire o usuário e a senha da URL: o receptor confere a assinatura de cada evento."
        )
    try:
        parts.port  # noqa: B018 (só valida: uma porta fora do intervalo levanta ValueError)
    except ValueError:
        raise InvalidUrl("A porta da URL não é válida.") from None
    if not parts.hostname:
        raise InvalidUrl("Falta o endereço do servidor na URL.")
    if parts.fragment:
        raise InvalidUrl("Tire o trecho depois do #: ele não chega ao receptor.")
    if _link_local(parts.hostname):
        raise InvalidUrl(
            "Endereços link-local (169.254.x.x, fe80::) são de metadados do servidor e não servem "
            "de destino."
        )
    if parts.scheme == "http" and production and not http_confirmed:
        raise InvalidUrl(
            "Em produção, um endereço http manda os dados das leads sem criptografia. Prefira "
            "https ou confirme abaixo que o receptor só aceita http.",
            needs_confirmation=True,
        )
    return url


def _link_local(hostname: str) -> bool:
    """O host é um IP link-local escrito na URL (169.254.0.0/16 ou fe80::/10, também dentro de um
    IPv6 que leva um IPv4). Um nome que resolve para um IP desses passa: a defesa completa contra
    SSRF fica para depois (SECURITY.md, "Limites conhecidos")."""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_link_local


@dataclass(frozen=True)
class Destination:
    """O destino que um envio usa, já com o segredo aberto. Fica só na memória do envio, e o
    `repr` não mostra o segredo (um log ou um traceback não o levam)."""

    scope: Scope
    url: str
    secret: str = field(repr=False)

    @property
    def host(self) -> str:
        return host_of(self.url)


@dataclass(frozen=True)
class StoredDestination:
    """Um destino como o admin mostra: sem o segredo."""

    id: uuid.UUID
    form_slug: str | None
    url: str
    version: int
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    secret_created_at: datetime
    events: tuple[str, ...] = ("plano.concluido",)
    purpose: str = "participant"
    purpose_origin: str = "default"

    @property
    def host(self) -> str:
        return host_of(self.url)


@dataclass(frozen=True)
class SaveResult:
    destination: StoredDestination
    # O segredo novo quando o destino acabou de nascer: mostre uma vez. Fora do `repr`, como o do
    # `Destination`: um log ou um traceback que imprime o resultado não o leva.
    secret: str | None = field(repr=False)


def host_of(url: str) -> str:
    """O host da URL, com a porta se houver (o que a entrega e a auditoria registram)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return f"{host}:{parts.port}" if parts.port else host


def origin_of(url: str) -> str:
    """O esquema e o host da URL, com a porta se houver (`https://host`), sem o caminho nem a
    consulta: o que quem só consulta pode ver, porque o caminho de uma URL de webhook costuma valer
    como senha. A URL já passou por `check_url` (sem usuário e senha)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def _stored(row: WebhookDestination) -> StoredDestination:
    return StoredDestination(
        id=row.id,
        form_slug=row.form_slug,
        url=row.url,
        version=row.version,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        secret_created_at=row.secret_created_at,
        events=tuple(row.events),
        purpose=row.purpose,
        purpose_origin=row.purpose_origin,
    )


def _same_scope(form_slug: str | None) -> sa.ColumnElement[bool]:
    column = WebhookDestination.form_slug
    return column.is_(None) if form_slug is None else column == form_slug


def _serving(form_slug: str) -> sa.ColumnElement[bool]:
    """Os destinos que podem servir o formulário: o dele e o padrão."""
    column = WebhookDestination.form_slug
    return sa.or_(column == form_slug, column.is_(None))


def _audit(
    session: Session,
    actor: str,
    action: str,
    row: WebhookDestination | StoredDestination,
    **details: object,
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=f"webhook_destination.{action}",
            target_type="webhook_destination",
            target_id=str(row.id),
            details={
                "form_slug": row.form_slug,
                "host": host_of(row.url),
                "purpose": row.purpose,
                "purpose_origin": row.purpose_origin,
                **details,
            },
        )
    )


def listing(session: Session) -> dict[str | None, StoredDestination]:
    """Todos os destinos guardados, pelo slug do formulário (None = o destino padrão)."""
    rows = session.scalars(sa.select(WebhookDestination))
    return {row.form_slug: _stored(row) for row in rows}


def serving(
    stored: dict[str | None, StoredDestination], form_slug: str
) -> StoredDestination | None:
    """Entre os destinos de `listing`, o que serve o formulário: o dele, senão o padrão. É a
    mesma regra de `resolve`, para a tela."""
    return stored.get(form_slug) or stored.get(None)


def unreadable(session: Session, cipher: SecretCipher | None) -> list[str | None]:
    """Os destinos (pelo slug; None = o padrão) cujo segredo a chave atual não abre: as entregas
    para eles falham até alguém gerar um segredo novo. Sem chave, nenhum abre."""
    rows = session.execute(
        sa.select(WebhookDestination.form_slug, WebhookDestination.secret_sealed)
    ).all()
    return [slug for slug, sealed in rows if cipher is None or not cipher.opens(sealed)]


def configured(session: Session, form_slug: str) -> bool:
    """O formulário tem para onde entregar: um destino próprio ou o padrão."""
    statement = sa.select(WebhookDestination.id).where(_serving(form_slug)).limit(1)
    return session.scalar(statement) is not None


def resolve(session: Session, cipher: SecretCipher | None, form_slug: str) -> Destination | None:
    """Resolve a configuração vigente para consulta legada: formulário, padrão ou None.
    A entrega PDF fixa e revalida sua própria autorização antes da rede; esta resolução isolada
    não autoriza transportar um documento. Levanta `UnreadableSecret` se o segredo não abre."""
    row = session.scalar(
        sa.select(WebhookDestination)
        .where(_serving(form_slug))
        .order_by(WebhookDestination.form_slug.is_(None))  # o do formulário vem antes
        .limit(1)
    )
    if row is None:
        return None
    if "plano.concluido" not in row.events:
        from app.errors import FailureCode, RetryableError

        raise RetryableError(
            FailureCode.DELIVERY_UNCONFIGURED, "destino não aceita evento de plano"
        )
    if cipher is None:
        raise UnreadableSecret
    scope: Scope = "default" if row.form_slug is None else "form"
    return Destination(scope=scope, url=row.url, secret=cipher.open(row.secret_sealed))


def save(
    session: Session,
    cipher: SecretCipher | None,
    *,
    form_slug: str | None,
    url: str,
    actor: str,
    expected_version: int,
    production: bool,
    http_confirmed: bool,
    events: list[str] | None = None,
    purpose: str | None = None,
) -> SaveResult:
    """Cria o destino (com um segredo novo, devolvido uma vez) ou muda a URL do que existe, que
    mantém o segredo. `expected_version` é a versão que a pessoa viu (0 = ainda não havia
    destino): se mudou, levanta `Conflict` e nada muda. URL recusada: `InvalidUrl`."""
    if events is not None and (
        not events
        or len(set(events)) != len(events)
        or set(events)
        - {
            "plano.concluido",
            "submissao.persistida",
            "plano.liberado",
            V2_SELECTION,
            "contato.capturado",
            "contato.revogado",
            "formulario.concluido",
        }
    ):
        raise InvalidEvents(
            "Selecione os eventos aceitos: plano.concluido, plano.liberado e/ou submissao.persistida."
        )
    from app.integration_readiness import configuration_lock

    configuration_lock(session)
    url = check_url(url, production=production, http_confirmed=http_confirmed)
    # Em produção, um http só passa confirmado: a auditoria guarda quem confirmou.
    insecure = production and urlsplit(url).scheme == "http"
    confirmed = {"http_confirmed": True} if insecure else {}
    current = session.scalar(
        sa.select(WebhookDestination).where(_same_scope(form_slug)).with_for_update()
    )
    chosen, origin = selected(
        purpose,
        current=current if expected_version else None,
        moved=current is not None and current.url != url,
    )
    effective_events = (
        events if events is not None else (current.events if current else ["plano.concluido"])
    )
    partial_events = {"contato.capturado", "formulario.concluido", "contato.revogado"}
    if partial_events.intersection(effective_events):
        if form_slug is None or not partial_events.issubset(effective_events):
            raise InvalidEvents(
                "Selecione contato capturado, revogado e formulário concluído juntos, no destino do formulário."
            )
        require_internal(chosen, receiver="O webhook de captura parcial")
    if V2_SELECTION in effective_events:
        from app.deployment_contract import DeploymentContractError, assert_contract

        if form_slug is None or "submissao.persistida" in effective_events:
            raise InvalidEvents(
                "Escolha respostas após salvar somente no destino do formulário, sem a seleção legada de captura."
            )
        require_internal(chosen, receiver="O envio de respostas")
        try:
            assert_contract(session, required=3)
        except DeploymentContractError:
            raise InvalidEvents(
                "A atualização ainda não permite enviar respostas após salvar. Aguarde a conclusão."
            ) from None
    if "plano.liberado" in effective_events:
        from app.deployment_contract import assert_contract

        assert_contract(session, required=2)
        if chosen != "participant":
            raise InvalidEvents("A notificação de liberação exige finalidade participante.")
    if "submissao.persistida" in effective_events:
        require_internal(chosen, receiver="O webhook de captura")
    if chosen == "participant":
        require_participant_capability(session)
    if expected_version == 0:
        if cipher is None:
            raise NoCipher
        secret = new_secret()
        row = _insert(session, cipher, form_slug=form_slug, url=url, secret=secret, actor=actor)
        if row is None:  # outro pedido criou o destino primeiro
            raise Conflict(_current(session, form_slug))
        row.purpose, row.purpose_origin = chosen, origin
        if events is not None:
            row.events = events
            session.flush()
        _audit(session, actor, "create", row, events=row.events, **confirmed)
        return SaveResult(_stored(row), secret)
    row = _lock_for_change(session, form_slug, expected_version, actor)
    previous_host = host_of(row.url)
    row.purpose, row.purpose_origin = chosen, origin
    row.url = url
    if events is not None:
        row.events = events
    _audit(
        session, actor, "update", row, previous_host=previous_host, events=row.events, **confirmed
    )
    return SaveResult(_stored(row), None)


def rotate(
    session: Session,
    cipher: SecretCipher | None,
    *,
    form_slug: str | None,
    actor: str,
    expected_version: int,
) -> str:
    """Troca o segredo do destino e devolve o novo, para mostrar uma vez. O antigo deixa de valer
    na hora: novos envios usam o novo. Entregas PDF fixadas antes da troca exigem repetição
    explícita com a revisão atual, preservando receptor e finalidade."""
    if cipher is None:
        raise NoCipher
    row = _lock_for_change(session, form_slug, expected_version, actor)
    secret = new_secret()
    row.secret_sealed = cipher.seal(secret)
    row.secret_created_at = row.updated_at
    _audit(session, actor, "rotate_secret", row)
    return secret


def sample_event(event_id: str, form_slug: str | None) -> dict[str, Any]:
    """O corpo do evento de teste: fictício, marcado como teste e sem nenhum dado de lead."""
    return {
        "event": TEST_EVENT,
        "event_id": event_id,
        "occurred_at": utcnow().isoformat(),
        "test": True,
        "form": {"slug": form_slug} if form_slug else None,
        "message": (
            "Evento de teste do Leadkit, mandado pelo painel admin. Não traz dados de "
            "lead: confira a assinatura e responda com um status 2xx."
        ),
    }


def _test_generation(session: Session, deps: Deps, form_slug: str) -> str:
    """Permite testar antes da primeira publicação sem alterar o rascunho ou publicar."""
    from app.forms import UnknownForm
    from app.forms.definition import DefinitionError, compile_definition
    from app.forms.drafts import load

    try:
        return deps.forms.current(form_slug).generation
    except UnknownForm:
        draft = load(session, form_slug)
        if draft is not None:
            try:
                return compile_definition(
                    draft.document, version_number=draft.next_number
                ).generation
            except DefinitionError:
                pass
        raise InvalidEvents(
            "Revise o rascunho do formulário antes de testar o envio de respostas; não é necessário publicar."
        ) from None


def send_test(
    deps: Deps, form_slug: str | None, *, actor: str, event: str = TEST_EVENT
) -> Probe | None:
    """Manda o evento de teste ao destino desta página (o padrão não entra no lugar do de um
    formulário), assinado com o segredo vigente, e devolve o que o receptor respondeu; None se o
    destino não existe. A auditoria guarda o status e o tempo, não a resposta. Levanta
    `UnreadableSecret` sem mandar nada se o segredo não abre."""
    from app.integration_readiness import configuration_lock

    with deps.sessions.begin() as session:
        configuration_lock(session, shared=True)
        purpose = session.scalar(
            sa.select(WebhookDestination.purpose).where(_same_scope(form_slug))
        )
        if purpose == "participant":
            require_participant_capability(session)
        row = session.scalar(
            sa.select(WebhookDestination).where(_same_scope(form_slug)).with_for_update()
        )
        if row is None:
            return None
        stored = _stored(row)
        if deps.cipher is None:
            raise UnreadableSecret
        secret = deps.cipher.open(row.secret_sealed)
        event_id = str(uuid.uuid4())
        payload = sample_event(event_id, form_slug)
        if event == V2_SELECTION:
            from app.deployment_contract import DeploymentContractError, assert_contract
            from app.models import Submission, WebhookIssuer
            from app.persisted_webhook import EVENT, build_payload

            require_internal(stored.purpose, receiver="O envio de respostas")
            if form_slug is None or V2_SELECTION not in stored.events:
                raise UnsupportedEvent
            try:
                assert_contract(session, required=3)
            except DeploymentContractError:
                raise InvalidPurpose(
                    "A atualização ainda não habilitou o envio de respostas."
                ) from None
            synthetic = Submission(
                id=uuid.uuid4(),
                form_slug=form_slug,
                form_version="teste",
                form_version_number=0,
                created_at=utcnow(),
                answers={},
                consent={},
            )
            payload = build_payload(
                delivery_id=uuid.UUID(event_id),
                issuer=session.get_one(WebhookIssuer, 1).namespace,
                submission=synthetic,
                generation=_test_generation(session, deps, form_slug),
                destination_id=stored.id,
                destination_revision=stored.version,
                correlation=None,
            )
            payload["test"] = True
            event = EVENT
        elif event == "submissao.persistida":
            require_internal(stored.purpose, receiver="O webhook de captura")
            if event not in stored.events:
                raise UnsupportedEvent
            payload = {
                "event": event,
                "event_id": event_id,
                "test": True,
                "occurred_at": utcnow().isoformat(),
                "form": {"slug": form_slug, "version": "teste", "version_number": 0},
                "submission": {"id": "teste-sintetico"},
                "lead": {"name": "Contadora Exemplo", "email": "teste@example.test", "phone": None},
                "answers": {},
                "consent": {},
                "tracking": {},
                "scores": {},
                "priority": None,
                "persona": None,
            }
        else:
            event = TEST_EVENT
        if stored.purpose == "participant":
            spec = deps.forms.current(form_slug) if form_slug else None
            if spec is None or spec.example_plan is None:
                raise InvalidPurpose(
                    "Para testar participante, abra o destino de um formulário PDF com exemplo de plano válido."
                )
            from app.forms import PlanProjectionError

            try:
                payload["plan"] = spec.project_plan(spec.example_plan, audience="participant")
            except PlanProjectionError:
                raise InvalidPurpose(
                    "O exemplo de plano não permite projeção participante. Corrija o formulário antes de testar."
                ) from None
            payload["audience"] = "participant"
        probe = deps.webhook.probe(stored.url, secret, event, event_id, payload)
        _audit(
            session,
            actor,
            "test",
            stored,
            status=probe.status,
            elapsed_ms=probe.elapsed_ms,
            error=probe.error,
        )
    return probe


def remove(
    session: Session, *, form_slug: str | None, actor: str, expected_version: int
) -> StoredDestination:
    """Apaga o destino e o devolve como estava (com a versão e a autoria da última mudança, não as
    da remoção). Envios novos passam a usar o padrão; intenções PDF fixadas não mudam de
    receptor. Somente fatos internos legados conservam a resolução legada por formulário/padrão."""
    from app.integration_readiness import configuration_lock

    configuration_lock(session)
    from app.integration_readiness import configuration_lock

    configuration_lock(session)
    row = _lock(session, form_slug, expected_version)
    removed = _stored(row)
    _audit(session, actor, "remove", row)
    session.delete(row)
    return removed


def _insert(
    session: Session,
    cipher: SecretCipher,
    *,
    form_slug: str | None,
    url: str,
    secret: str,
    actor: str,
) -> WebhookDestination | None:
    """O destino novo; None se já havia um para o mesmo formulário (ou um padrão)."""
    now = utcnow()
    return session.scalar(
        insert(WebhookDestination)
        .values(
            id=uuid.uuid4(),
            form_slug=form_slug,
            url=url,
            secret_sealed=cipher.seal(secret),
            version=1,
            created_by=actor,
            created_at=now,
            updated_by=actor,
            updated_at=now,
            secret_created_at=now,
        )
        .on_conflict_do_nothing()  # vale para o slug único e para o padrão único
        .returning(WebhookDestination)
    )


def _lock(session: Session, form_slug: str | None, expected_version: int) -> WebhookDestination:
    """O destino travado, se ainda está na versão que a pessoa viu (senão `Conflict`)."""
    row = session.scalar(
        sa.select(WebhookDestination)
        .where(_same_scope(form_slug), WebhookDestination.version == expected_version)
        .with_for_update()
    )
    if row is None:
        raise Conflict(_current(session, form_slug))
    return row


def _lock_for_change(
    session: Session, form_slug: str | None, expected_version: int, actor: str
) -> WebhookDestination:
    """O destino travado para mudar, se ainda está na versão que a pessoa viu (senão
    `Conflict`), já com a versão nova e a autoria da mudança."""
    from app.integration_readiness import configuration_lock

    configuration_lock(session)
    row = _lock(session, form_slug, expected_version)
    row.version += 1
    row.updated_by = actor
    row.updated_at = utcnow()
    return row


def _current(session: Session, form_slug: str | None) -> StoredDestination | None:
    row = session.scalar(sa.select(WebhookDestination).where(_same_scope(form_slug)))
    return _stored(row) if row else None


# --- subida da API e do worker ----------------------------------------------------------------


def prepare(deps: Deps) -> None:
    """Roda na subida da API e do worker, antes de servir. Idempotente e seguro com os dois
    subindo juntos.

    1. Na primeira subida depois da migration, WEBHOOK_URL e WEBHOOK_SECRET viram o destino
       padrão, se ainda não houver um. Uma marca no banco registra essa subida: daí em diante o
       ambiente é ignorado, mesmo que o destino padrão seja removido no admin.
    2. Em produção, sem WEBHOOK_ENCRYPTION_KEY e com algum destino guardado, a subida para.
    3. Os segredos cifrados com uma chave que não é a primeira de WEBHOOK_ENCRYPTION_KEY são
       cifrados de novo com ela: depois de uma subida, a chave antiga pode sair do ambiente.

    Banco ainda sem as tabelas (no primeiro deploy da stack do Swarm, a API e o worker sobem
    antes das migrations): não faz nada, e a importação fica para a próxima subida.
    """
    with deps.sessions() as session:
        migrated = sa.inspect(session.connection()).has_table(WebhookEnvSeed.__tablename__)
    if not migrated:
        logger.warning(
            "destino do webhook: a migration ainda não rodou; a importação do WEBHOOK_URL fica "
            "para a próxima subida"
        )
        return
    with deps.sessions.begin() as session:
        _seed_from_env(session, deps)
    with deps.sessions.begin() as session:
        _check_secrets(session, deps)


def _seed_from_env(session: Session, deps: Deps) -> None:
    settings = deps.settings
    first_start = session.scalar(
        insert(WebhookEnvSeed)
        .values(id=1, at=utcnow())
        .on_conflict_do_nothing()  # o outro processo, subindo junto, espera este terminar
        .returning(WebhookEnvSeed.id)
    )
    if first_start is None:
        if settings.webhook_url:
            logger.info(
                "destino do webhook: WEBHOOK_URL e WEBHOOK_SECRET ignorados; o destino fica no admin"
            )
        return
    if not (settings.webhook_url and settings.webhook_secret):
        return
    if deps.cipher is None:  # sem a chave, a marca não fica e a próxima subida tenta de novo
        raise StartupError(
            "WEBHOOK_URL e WEBHOOK_SECRET viram o destino padrão na primeira subida e precisam de "
            "WEBHOOK_ENCRYPTION_KEY para guardar o segredo (como gerar: app/README.md)"
        )
    try:
        # O ambiente é a confirmação de quem configurou o servidor, inclusive para um http.
        url = check_url(
            settings.webhook_url,
            production=settings.app_env == "production",
            http_confirmed=True,
        )
    except InvalidUrl as exc:
        message = f"WEBHOOK_URL não serve como destino do webhook: {exc.message}"
        raise StartupError(message) from None
    chosen, origin = selected(settings.webhook_purpose)
    if chosen == "participant":
        try:
            require_participant_capability(session)
        except InvalidPurpose:
            raise StartupError(
                "WEBHOOK_PURPOSE=internal exige escolha explícita para uso interno. Para participante, deixe WEBHOOK_URL e WEBHOOK_SECRET vazios no bootstrap, conclua a verificação de compatibilidade e configure o destino no admin."
            ) from None
    row = _insert(
        session,
        deps.cipher,
        form_slug=None,
        url=url,
        secret=settings.webhook_secret,
        actor=ENV_ACTOR,
    )
    if row is None:  # a API subiu antes da migration e alguém já configurou o padrão no admin
        return
    row.purpose, row.purpose_origin = chosen, origin
    _audit(session, ENV_ACTOR, "create", row, source="WEBHOOK_URL")
    logger.info("destino do webhook: o WEBHOOK_URL virou o destino padrão host=%s", host_of(url))


def _check_secrets(session: Session, deps: Deps) -> None:
    rows = session.execute(
        sa.select(
            WebhookDestination.id, WebhookDestination.form_slug, WebhookDestination.secret_sealed
        )
    ).all()
    if not rows:
        return
    production = deps.settings.app_env == "production"
    if deps.cipher is None:
        if production:
            raise StartupError(
                "WEBHOOK_ENCRYPTION_KEY é obrigatória em produção quando há destino do webhook: "
                "sem ela, nenhum segredo abre e nada é entregue"
            )
        logger.warning("destino do webhook: sem WEBHOOK_ENCRYPTION_KEY, nada é entregue")
        return
    closed = unreadable(session, deps.cipher)
    if production and len(closed) == len(rows):  # a chave é outra: a de antes se perdeu?
        raise StartupError(
            "WEBHOOK_ENCRYPTION_KEY não abre o segredo de nenhum destino do webhook: confira a "
            "chave (se ela se perdeu, veja como recuperar em SECURITY.md)"
        )
    for form_slug in closed:  # só alguns: a subida segue, e o admin avisa
        logger.warning(
            "destino do webhook formulario=%s: o segredo não abre com WEBHOOK_ENCRYPTION_KEY; "
            "gere um novo no admin",
            form_slug or "(padrão)",
        )
    for destination_id, form_slug, sealed in rows:
        if form_slug in closed:
            continue
        resealed = deps.cipher.reseal(sealed)
        if resealed is not None:  # só se ninguém trocou o segredo no meio do caminho
            session.execute(
                sa.update(WebhookDestination)
                .where(
                    WebhookDestination.id == destination_id,
                    WebhookDestination.secret_sealed == sealed,
                )
                .values(secret_sealed=resealed)
            )
