from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JOB_STATUSES = ("queued", "running", "completed", "failed")
JOB_STAGES = ("queued", "generating", "rendering", "completed", "failed")
DELIVERY_STATUSES = ("pending", "succeeded", "failed")
WEBHOOK_SCOPES = ("form", "default")  # o destino do formulário ou o padrão
DRAFT_TEST_STATUSES = ("queued", "running", "succeeded", "failed")
INTEGRATION_KINDS = ("baserow",)  # os tipos de integração de planilha por formulário
SYNC_STATUSES = ("pending", "succeeded", "failed")
ASSET_CONTENT_TYPES = ("image/png", "image/jpeg", "image/webp")
# Do papel de menos poder para o de mais (`app.admin.api.keys.allows` conta com essa ordem): só
# consulta, ou tudo o que o admin logado faz.
API_KEY_ROLES = ("leitura", "total")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING)


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Lead(Base):
    """Pessoa identificada pelo e-mail normalizado (chave de deduplicação entre formulários)."""

    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(sa.Text, unique=True)
    name: Mapped[str | None] = mapped_column(sa.Text)
    phone: Mapped[str | None] = mapped_column(sa.Text)
    first_touch: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Submission(Base):
    """Resposta válida a um formulário. Respostas rejeitadas não são gravadas."""

    __tablename__ = "submissions"
    __table_args__ = (
        sa.UniqueConstraint("form_slug", "idempotency_key", name="uq_submissions_form_idempotency"),
    )

    id: Mapped[uuid.UUID] = _pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("leads.id"), index=True)
    form_slug: Mapped[str] = mapped_column(sa.String(64))
    form_version: Mapped[str] = mapped_column(sa.String(64))
    # O número da versão no registro com que a lead respondeu (`FormSpec.version_number`): o
    # pipeline, o admin e a página de resultado seguem essa versão. `form_version` guarda o texto
    # `version`, que vai no webhook. O default 1 preserva a compatibilidade dos envios importados
    # que não informavam explicitamente o número da versão.
    form_version_number: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("1"))
    idempotency_key: Mapped[str] = mapped_column(sa.String(200))
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB)
    consent: Mapped[dict[str, Any]] = mapped_column(JSONB)
    tracking: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    scores: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    priority: Mapped[str | None] = mapped_column(sa.String(16), index=True)
    persona: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = _created_at()


class CaptureReceipt(Base):
    """Capacidade revogável para ler apenas a conclusão histórica de uma captura."""

    __tablename__ = "capture_receipts"
    id: Mapped[uuid.UUID] = _pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("submissions.id", ondelete="CASCADE"), unique=True
    )
    public_id: Mapped[str] = mapped_column(sa.String(32), unique=True)
    token_version: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("1"))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class Job(Base):
    """Estado de negócio do pedido de plano. Execução, retries e histórico ficam no Hatchet."""

    __tablename__ = "jobs"
    __table_args__ = (
        sa.CheckConstraint(f"status in ({_in(JOB_STATUSES)})", name="status"),
        sa.CheckConstraint(f"stage in ({_in(JOB_STAGES)})", name="stage"),
        sa.Index("ix_jobs_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("submissions.id"), unique=True)
    public_id: Mapped[str] = mapped_column(sa.String(32), unique=True)
    status: Mapped[str] = mapped_column(sa.String(16), default="queued")
    stage: Mapped[str] = mapped_column(sa.String(16), default="queued")
    attempt: Mapped[int] = mapped_column(sa.Integer, default=1)
    token_version: Mapped[int] = mapped_column(sa.Integer, default=1)
    hatchet_run_id: Mapped[str | None] = mapped_column(sa.String(64))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(sa.String(40))
    failure_detail: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Plan(Base):
    """Plano gerado pelo LLM e já validado. Uma linha por tentativa (reprocessar cria nova versão)."""

    __tablename__ = "plans"
    __table_args__ = (sa.UniqueConstraint("job_id", "version", name="uq_plans_job_version"),)

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("jobs.id"), index=True)
    version: Mapped[int] = mapped_column(sa.Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(sa.Text)
    prompt_version: Mapped[str] = mapped_column(sa.Text)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created_at()


class Artifact(Base):
    """PDF guardado no storage, com a chave `plans/{job}/v{versão}.pdf` (as antigas levam o hash no
    nome; quem lê ou apaga o arquivo usa a chave desta linha). `purged_at`: a retenção apagou o
    arquivo (app.retention); a linha fica até o pedido sair."""

    __tablename__ = "artifacts"
    __table_args__ = (
        sa.UniqueConstraint("job_id", "kind", "version", name="uq_artifacts_job_kind_version"),
    )

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("jobs.id"), index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("plans.id"))
    kind: Mapped[str] = mapped_column(sa.String(16), default="pdf")
    version: Mapped[int] = mapped_column(sa.Integer)
    storage_key: Mapped[str] = mapped_column(sa.Text, unique=True)
    sha256: Mapped[str] = mapped_column(sa.String(64))
    byte_size: Mapped[int] = mapped_column(sa.Integer)
    renderer: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    purged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    storage_revision: Mapped[str | None] = mapped_column(sa.Text)


class Delivery(Base):
    """Fato PDF com receptor, revisão, audiência e tentativa fixados; epochs cercam repetições.

    Históricos sem correlação são internos legados e nunca adotam participante por fallback.
    Escopo e host registram o último transporte; a exclusão do receptor não apaga o fato nem
    autoriza outro destino. Nenhuma URL, segredo ou cópia do documento vive nesta linha."""

    __tablename__ = "deliveries"
    __table_args__ = (
        sa.CheckConstraint("audience in ('internal', 'participant')", name="audience"),
        sa.CheckConstraint(
            "authorization_origin in ('legacy', 'explicit')", name="authorization_origin"
        ),
        sa.CheckConstraint(
            "authorization_origin = 'legacy' AND audience = 'internal' OR authorization_origin = 'explicit' AND webhook_destination_id IS NOT NULL AND config_version IS NOT NULL AND authorized_attempt IS NOT NULL",
            name="authorization",
        ),
        sa.UniqueConstraint(
            "artifact_id", "destination", name="uq_deliveries_artifact_destination"
        ),
        sa.CheckConstraint(f"status in ({_in(DELIVERY_STATUSES)})", name="status"),
        sa.CheckConstraint(
            f"destination_scope in ({_in(WEBHOOK_SCOPES)})", name="destination_scope"
        ),
        sa.CheckConstraint(
            "(destination = 'webhook.release' AND event = 'plano.liberado' "
            "AND release_id IS NOT NULL AND audience = 'participant' "
            "AND authorization_origin = 'explicit' AND authorized_attempt IS NOT NULL "
            "AND webhook_destination_id IS NOT NULL AND config_version IS NOT NULL) OR "
            "(destination <> 'webhook.release' AND event <> 'plano.liberado' AND release_id IS NULL)",
            name="release_channel",
        ),
        sa.Index(
            "uq_deliveries_release_receiver",
            "release_id",
            "webhook_destination_id",
            unique=True,
            postgresql_where=sa.text("release_id IS NOT NULL"),
        ),
        sa.Index("ix_deliveries_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("jobs.id"), index=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("artifacts.id"))
    webhook_destination_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    config_version: Mapped[int | None] = mapped_column(sa.Integer)
    audience: Mapped[str] = mapped_column(sa.String(16), server_default="internal")
    authorization_origin: Mapped[str] = mapped_column(sa.String(16), server_default="legacy")
    authorized_attempt: Mapped[int | None] = mapped_column(sa.Integer)
    dispatch_no: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    release_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("plan_releases.id"))
    occurred_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    blocked_reason: Mapped[str | None] = mapped_column(sa.Text)
    destination: Mapped[str] = mapped_column(sa.String(64))
    event: Mapped[str] = mapped_column(sa.String(64))
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0)
    hatchet_run_id: Mapped[str | None] = mapped_column(sa.String(64))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    response_status: Mapped[int | None] = mapped_column(sa.Integer)
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    destination_scope: Mapped[str | None] = mapped_column(sa.String(16))
    destination_host: Mapped[str | None] = mapped_column(sa.Text)


class WebhookDestination(Base):
    """Destino do webhook: o padrão (`form_slug` nulo, no máximo um) ou o de um formulário (no
    máximo um por slug). O segredo fica cifrado com WEBHOOK_ENCRYPTION_KEY (`app.destinations`);
    `version` sobe a cada mudança, para o admin recusar um formulário aberto antes dela."""

    __tablename__ = "webhook_destinations"
    __table_args__ = (
        sa.CheckConstraint("purpose in ('internal', 'participant')", name="purpose"),
        sa.CheckConstraint(
            "purpose_origin in ('legacy', 'explicit', 'default')", name="purpose_origin"
        ),
        sa.UniqueConstraint("form_slug", name="uq_webhook_destinations_form_slug"),
        sa.Index(
            "uq_webhook_destinations_default",
            sa.text("(form_slug IS NULL)"),
            unique=True,
            postgresql_where=sa.text("form_slug IS NULL"),
        ),
    )

    purpose: Mapped[str] = mapped_column(sa.String(16), server_default="participant")
    purpose_origin: Mapped[str] = mapped_column(sa.String(16), server_default="default")

    id: Mapped[uuid.UUID] = _pk()
    form_slug: Mapped[str | None] = mapped_column(sa.String(64))
    url: Mapped[str] = mapped_column(sa.Text)
    secret_sealed: Mapped[str] = mapped_column(sa.Text)
    events: Mapped[list[str]] = mapped_column(
        JSONB, server_default=sa.text("'[\"plano.concluido\"]'::jsonb")
    )
    version: Mapped[int] = mapped_column(sa.Integer, default=1)
    created_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_by: Mapped[str] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    secret_created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )


class WebhookEnvSeed(Base):
    """Marca da primeira subida depois da migration do destino no admin: o WEBHOOK_URL e o
    WEBHOOK_SECRET do ambiente já foram levados para o destino padrão (ou não havia o que levar;
    a auditoria registra quando houve). Uma linha só; com ela, o ambiente é ignorado."""

    __tablename__ = "webhook_env_seed"
    __table_args__ = (sa.CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(sa.SmallInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)


class Integration(Base):
    """Integração de um formulário com uma planilha externa (hoje só o Baserow, `app.integrations`):
    no máximo uma de cada tipo por formulário, pelo slug. Cada pedido concluído do formulário vira
    uma linha na tabela, pela chave (o código do pedido).

    `mapping` é a lista, em ordem, de `{field_id, name, type, source}`: qual dado do envio preenche
    cada campo da tabela (`app.integrations.mapping`). `key_field_id` é o campo que guarda o código
    do pedido, pelo qual o envio acha a linha de volta (o campo mapeado ao código do pedido). O
    token do Baserow fica cifrado com WEBHOOK_ENCRYPTION_KEY, como o segredo de um destino, e nunca
    sai do banco inteiro. `active` liga o envio; `version` sobe a cada mudança, para o admin recusar
    uma página aberta antes dela."""

    __tablename__ = "integrations"
    __table_args__ = (
        sa.CheckConstraint("purpose in ('internal', 'participant')", name="purpose"),
        sa.CheckConstraint(
            "purpose_origin in ('legacy', 'explicit', 'default')", name="purpose_origin"
        ),
        sa.UniqueConstraint("form_slug", "kind", name="uq_integrations_form_slug_kind"),
        sa.CheckConstraint(f"kind in ({_in(INTEGRATION_KINDS)})", name="kind"),
        sa.CheckConstraint("table_id >= 1", name="table_id"),
    )

    purpose: Mapped[str] = mapped_column(sa.String(16), server_default="participant")
    purpose_origin: Mapped[str] = mapped_column(sa.String(16), server_default="default")

    id: Mapped[uuid.UUID] = _pk()
    form_slug: Mapped[str] = mapped_column(sa.String(64))
    kind: Mapped[str] = mapped_column(sa.String(16))
    base_url: Mapped[str] = mapped_column(sa.Text)
    table_id: Mapped[int] = mapped_column(sa.Integer)
    token_sealed: Mapped[str] = mapped_column(sa.Text)
    mapping: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    key_field_id: Mapped[int | None] = mapped_column(sa.Integer)
    capture_mapping: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=sa.text("'[]'::jsonb")
    )
    capture_key_field_id: Mapped[int | None] = mapped_column(sa.Integer)
    active: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    version: Mapped[int] = mapped_column(sa.Integer, default=1)
    created_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_by: Mapped[str] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )
    token_changed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )


class IntegrationSync(Base):
    """O envio de um pedido concluído a uma integração (`app.integrations.syncs`), o par da
    `Delivery` do webhook: nasce `pending` na mesma transação que conclui o pedido, uma por (integração,
    PDF), e o Hatchet a leva a `succeeded` ou `failed`. Não guarda dado da lead: o que vai à planilha
    é montado na hora do envio. `row_id` é o id da linha no Baserow; `outcome`, se o envio criou a
    linha ou atualizou a que já existia."""

    __tablename__ = "integration_syncs"
    __table_args__ = (
        sa.UniqueConstraint("integration_id", "artifact_id", name="uq_integration_syncs_artifact"),
        sa.CheckConstraint(f"status in ({_in(SYNC_STATUSES)})", name="status"),
        sa.Index("ix_integration_syncs_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    integration_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("integrations.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("jobs.id"), index=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("artifacts.id"))
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0)
    hatchet_run_id: Mapped[str | None] = mapped_column(sa.String(64))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    outcome: Mapped[str | None] = mapped_column(sa.String(16))
    row_id: Mapped[int | None] = mapped_column(sa.Integer)
    synced_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class CaptureDelivery(Base):
    """Intenção por submissão/destino; v2 conserva snapshot sob a cascata da submissão."""

    __tablename__ = "capture_deliveries"
    __table_args__ = (
        sa.CheckConstraint(
            "(schema_version = 1 AND payload IS NULL) OR "
            "(schema_version = 2 AND integration_id IS NULL AND payload IS NOT NULL "
            "AND payload @> jsonb_build_object('schema_version', 2, 'event', event, "
            "'event_id', id::text, 'submission', jsonb_build_object('id', submission_id::text), "
            "'delivery_authorization', jsonb_build_object('destination_id', "
            "webhook_destination_id::text, 'revision', config_version, 'purpose', 'internal')))",
            name="envelope",
        ),
        sa.CheckConstraint(
            "(integration_id IS NULL) <> (webhook_destination_id IS NULL)", name="destination"
        ),
        sa.CheckConstraint(f"status in ({_in(DELIVERY_STATUSES)})", name="status"),
        sa.UniqueConstraint(
            "submission_id", "event", "integration_id", name="uq_capture_deliveries_integration"
        ),
        sa.UniqueConstraint(
            "submission_id", "event", "webhook_destination_id", name="uq_capture_deliveries_webhook"
        ),
        sa.Index("ix_capture_deliveries_status_updated_at", "status", "updated_at"),
    )
    id: Mapped[uuid.UUID] = _pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("integrations.id", ondelete="CASCADE"), index=True
    )
    webhook_destination_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(
            "webhook_destinations.id", ondelete="CASCADE", name="fk_capture_deliveries_webhook"
        ),
        index=True,
    )
    event: Mapped[str] = mapped_column(sa.String(64), default="submissao.persistida")
    schema_version: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    config_version: Mapped[int] = mapped_column(sa.Integer)
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0)
    dispatch_no: Mapped[int] = mapped_column(sa.Integer, default=1)
    hatchet_run_id: Mapped[str | None] = mapped_column(sa.String(64))
    last_error: Mapped[str | None] = mapped_column(sa.String(64))
    response_status: Mapped[int | None] = mapped_column(sa.Integer)
    row_id: Mapped[int | None] = mapped_column(sa.Integer)
    outcome: Mapped[str | None] = mapped_column(sa.String(16))
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Form(Base):
    """Formulário guardado no banco (ADR 0013): o slug, que é o endereço público (`/p/<slug>`), o
    título e a situação. O conteúdo fica nas versões (`FormVersion`); a atual é a de maior número.

    `title` é o nome do formulário no admin, que nasce do título da definição; a página pública e
    o PDF usam o título da versão. `active` é a situação: inativo, o formulário não recebe envios
    novos, e os que já existem seguem a versão deles (`app.forms.stored`).

    Um formulário criado do zero ou duplicado no admin nasce aqui sem versão, só com o rascunho
    (`FormDraft`), e não recebe envios até a primeira publicação. Até ela, o slug ainda pode mudar.
    """

    __tablename__ = "forms"
    __table_args__ = (
        sa.CheckConstraint(
            "retention_pdf_days IS NULL OR retention_pdf_days >= 1", name="retention_pdf_days"
        ),
        sa.CheckConstraint("retention_days IS NULL OR retention_days >= 1", name="retention_days"),
    )

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(sa.String(64), unique=True)
    title: Mapped[str] = mapped_column(sa.Text)
    active: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    # Os prazos de retenção próprios do formulário (`app.forms.retention`): o do arquivo do PDF,
    # contado da geração, e o do pedido inteiro, contado da última mudança do job. Nulo: o padrão do
    # ambiente (`RETENTION_PDF_DAYS`, `RETENTION_DAYS`). Moram no registro, não na versão: são
    # política de operação, valem para os pedidos que já existem e mudar um não pede publicação.
    retention_pdf_days: Mapped[int | None] = mapped_column(sa.Integer)
    retention_days: Mapped[int | None] = mapped_column(sa.Integer)
    created_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()


class FormVersion(Base):
    """Versão publicada de um formulário do banco: o número, sequencial por formulário a partir de
    1, e a definição inteira num documento só (`app.forms.definition`). Imutável: o banco recusa
    UPDATE e DELETE nesta tabela (migration 0005), e o rascunho mora fora dela (`FormDraft`). A
    versão 1 nasce da importação; as seguintes, da publicação do rascunho (`app.forms.drafts`).

    `fingerprint` é a impressão digital do conteúdo: o sha256 da definição em JSON canônico
    (`app.forms.definition.fingerprint`)."""

    __tablename__ = "form_versions"
    __table_args__ = (
        sa.UniqueConstraint("form_id", "number", name="uq_form_versions_form_number"),
        sa.CheckConstraint("number >= 1", name="number"),
    )

    id: Mapped[uuid.UUID] = _pk()
    form_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("forms.id"))
    number: Mapped[int] = mapped_column(sa.Integer)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(sa.String(64))
    note: Mapped[str] = mapped_column(sa.Text)
    published_by: Mapped[str] = mapped_column(sa.Text)
    published_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )


class FormDraft(Base):
    """O rascunho de um formulário do banco, um só por formulário (`app.forms.drafts`): a
    definição que a operadora edita sem afetar o que está no ar. A linha existe só enquanto o
    rascunho difere da versão atual: nasce na primeira edição que muda algo, sai quando a operadora
    salva de volta o conteúdo publicado e sai na publicação, quando vira a versão seguinte. Um
    rascunho guardado sempre compila: salvar confere a definição inteira e recusa o que não compila.

    `base_version` é a versão de onde ele foi copiado: a atual na primeira edição, ou a escolhida
    ao voltar a uma versão. Num formulário criado do zero ou duplicado no admin, o rascunho nasce
    antes da versão 1 e fica sem `base_version` até a primeira publicação, que o leva embora como
    a versão 1. `fingerprint` é a impressão digital da definição, que o editor devolve
    para o admin recusar uma página aberta antes da última mudança. `tested_fingerprint` e
    `tested_at` dizem o que ia ao LLM no último teste com sucesso e quando: a prova que a publicação
    pede, e que fica mesmo depois de a retenção apagar o resultado do teste."""

    __tablename__ = "form_drafts"
    __table_args__ = (sa.CheckConstraint("base_version >= 1", name="base_version"),)

    form_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("forms.id"), primary_key=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(sa.String(64))
    base_version: Mapped[int | None] = mapped_column(sa.Integer)
    updated_by: Mapped[str] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    tested_fingerprint: Mapped[str | None] = mapped_column(sa.String(64))
    tested_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class DraftTest(Base):
    """Um teste do rascunho (`app.forms.drafts`): uma chamada de verdade ao LLM e o PDF, com as
    respostas que a operadora escolheu, sem envio, pedido nem entrega. A linha faz o papel de
    outbox, como `jobs`: guarda a cópia da definição testada, as respostas e a data simulada, e a
    task `draft_test` do worker recebe só o id. `status` vai de `queued` a `running` (com
    `claimed_at`) e termina em `succeeded` ou `failed`; o índice parcial deixa um só teste em
    andamento por formulário.

    Guarda o que a tela mostra: as instruções e o prompt como foram ao LLM, o plano validado, o
    PDF, o tempo, os tokens e as rodadas de reparo (`usage`, como no plano de um pedido).
    `llm_fingerprint` é a impressão digital do que vai ao LLM ou o valida: a publicação aceita um
    teste com sucesso com a mesma. As respostas são da operadora, fictícias; ficam só os testes
    mais recentes de cada formulário, e a retenção apaga os de mais de alguns dias."""

    __tablename__ = "draft_tests"
    __table_args__ = (
        sa.CheckConstraint(f"status in ({_in(DRAFT_TEST_STATUSES)})", name="status"),
        sa.Index("ix_draft_tests_form_id_started_at", "form_id", "started_at"),
        sa.Index(
            "uq_draft_tests_in_progress",
            "form_id",
            unique=True,
            postgresql_where=sa.text("status in ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    form_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("forms.id"))
    status: Mapped[str] = mapped_column(sa.String(16))
    started_by: Mapped[str] = mapped_column(sa.Text)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    simulated_date: Mapped[date | None] = mapped_column(sa.Date)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    definition_fingerprint: Mapped[str] = mapped_column(sa.String(64))
    llm_fingerprint: Mapped[str] = mapped_column(sa.String(64))
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB)
    instructions: Mapped[str] = mapped_column(sa.Text)
    prompt: Mapped[str] = mapped_column(sa.Text)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(sa.Text)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    llm_ms: Mapped[int | None] = mapped_column(sa.Integer)
    pdf_ms: Mapped[int | None] = mapped_column(sa.Integer)
    pdf: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    pdf_pages: Mapped[int | None] = mapped_column(sa.Integer)
    failure_code: Mapped[str | None] = mapped_column(sa.String(40))
    failure_detail: Mapped[str | None] = mapped_column(sa.Text)


class DraftTestCommand(Base):
    """Vínculo técnico sem conteúdo, independente da retenção do resultado do teste."""

    __tablename__ = "draft_test_commands"
    __table_args__ = (
        sa.CheckConstraint("principal_kind in ('admin', 'api_key')", name="principal_kind"),
        sa.CheckConstraint(
            "dispatch_state in ('committed', 'dispatching', 'enqueued', 'not_started', 'uncertain')",
            name="dispatch_state",
        ),
        sa.Index("ix_draft_test_commands_expires_at", "expires_at"),
    )
    command_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    namespace: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    form_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    form_slug: Mapped[str] = mapped_column(sa.Text)
    expected_version: Mapped[str] = mapped_column(sa.Text)
    principal_kind: Mapped[str] = mapped_column(sa.String(16))
    principal_id: Mapped[str] = mapped_column(sa.Text)
    input_digest: Mapped[str] = mapped_column(sa.String(64))
    effective_digest: Mapped[str] = mapped_column(sa.String(64))
    issued_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    committed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    test_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    audit_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    channel: Mapped[str] = mapped_column(sa.String(16))
    dispatch_state: Mapped[str] = mapped_column(sa.String(16), default="committed")
    dispatch_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class FormAsset(Base):
    """Uma imagem de formulário (`app.forms.assets`): conteúdo que os templates do PDF e da prévia
    pedem por nome (`asset:<nome>`). Os bytes ficam no storage, pela chave do conteúdo
    (`assets/<sha256>.<extensão>`), imutáveis e sem duplicata; aqui ficam os metadados.

    `form_id` nulo: a biblioteca geral, que todo formulário enxerga; preenchido, a imagem é só
    daquele formulário e vale mais que a da biblioteca com o mesmo nome. O nome é único no escopo.
    `source_size` é o tamanho do arquivo enviado, antes da otimização (`app.forms.image_optimizer`),
    para a tela mostrar o antes e o depois. Trocar a imagem muda o conteúdo e deixa o mesmo nome,
    e `updated_by` e `updated_at` dizem quem trocou e quando."""

    __tablename__ = "form_assets"
    __table_args__ = (
        sa.UniqueConstraint("form_id", "name", name="uq_form_assets_form_id_name"),
        # No Postgres os nulos são todos diferentes: sem este índice, a biblioteca aceitaria dois
        # nomes iguais.
        sa.Index(
            "uq_form_assets_library_name",
            "name",
            unique=True,
            postgresql_where=sa.text("form_id IS NULL"),
        ),
        sa.Index("ix_form_assets_sha256", "sha256"),
        sa.CheckConstraint(f"content_type in ({_in(ASSET_CONTENT_TYPES)})", name="content_type"),
        sa.CheckConstraint("byte_size >= 1", name="byte_size"),
    )

    id: Mapped[uuid.UUID] = _pk()
    form_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("forms.id"))
    name: Mapped[str] = mapped_column(sa.String(48))
    content_type: Mapped[str] = mapped_column(sa.String(32))
    byte_size: Mapped[int] = mapped_column(sa.Integer)
    source_size: Mapped[int] = mapped_column(sa.Integer)
    width: Mapped[int] = mapped_column(sa.Integer)
    height: Mapped[int] = mapped_column(sa.Integer)
    sha256: Mapped[str] = mapped_column(sa.String(64))
    storage_key: Mapped[str] = mapped_column(sa.Text)
    created_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_by: Mapped[str] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )


class FormVersionAsset(Base):
    """A imagem que uma versão publicada usa, fixada no conteúdo que ela tinha na publicação: o
    sha256 e o tipo, para um PDF antigo poder ser refeito igual mesmo depois de a imagem ser
    trocada. `asset_id` aponta para a imagem de onde saiu: enquanto alguma versão a usa, o banco
    não deixa removê-la. Gravada na publicação (e na importação), junto com a versão, e nunca
    muda."""

    __tablename__ = "form_version_assets"
    __table_args__ = (sa.Index("ix_form_version_assets_sha256", "sha256"),)

    form_version_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("form_versions.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(sa.String(48), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("form_assets.id"), index=True)
    sha256: Mapped[str] = mapped_column(sa.String(64))
    content_type: Mapped[str] = mapped_column(sa.String(32))


class AuditLog(Base):
    """Ações humanas (reprocessar, reenviar, mudar um destino do webhook, importar, ativar e
    desativar um formulário, salvar e testar o rascunho, publicar e voltar a uma versão). Nunca
    guarda respostas, dados da lead, o valor de um segredo nem o conteúdo de uma definição."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _pk()
    at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    actor: Mapped[str] = mapped_column(sa.Text)
    action: Mapped[str] = mapped_column(sa.Text)
    target_type: Mapped[str | None] = mapped_column(sa.Text)
    target_id: Mapped[str | None] = mapped_column(sa.Text, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ApiKey(Base):
    """Uma chave de API do admin (`app.admin.api.keys`), com nome e papel. O segredo nunca é
    guardado: só o SHA-256 dele (`secret_hash`), e a chave inteira (`gp_<key_id>_<segredo>`) aparece
    uma única vez, na resposta da criação.

    `key_id` é a parte pública da chave, só para achar a linha. `name` é único, não muda e não se
    reaproveita, nem o de uma chave revogada: a auditoria cita o autor como `chave:<nome>`, e a
    linha revogada fica justamente para o nome continuar dizendo quem foi. `expires_at` nulo é uma
    chave sem validade. Revogar grava `revoked_at` e `revoked_by`, e a linha nunca é apagada.
    `last_used_at` é gravado no máximo uma vez por minuto."""

    __tablename__ = "api_keys"
    __table_args__ = (sa.CheckConstraint(f"role in ({_in(API_KEY_ROLES)})", name="role"),)

    id: Mapped[uuid.UUID] = _pk()
    key_id: Mapped[str] = mapped_column(sa.String(8), unique=True)
    name: Mapped[str] = mapped_column(sa.String(60), unique=True)
    role: Mapped[str] = mapped_column(sa.String(16))
    secret_hash: Mapped[str] = mapped_column(sa.String(64))
    created_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(sa.Text)
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class WebhookIssuer(Base):
    """Namespace persistido da instalação, conservado no backup, sem mutação operacional."""

    __tablename__ = "webhook_issuer"
    __table_args__ = (sa.CheckConstraint("id = 1", name="single_row"),)
    id: Mapped[int] = mapped_column(sa.SmallInteger, primary_key=True)
    namespace: Mapped[uuid.UUID] = mapped_column(sa.Uuid)


class DeploymentContract(Base):
    """Piso operacional persistente e atestação da frota compatível com projeções."""

    __tablename__ = "deployment_contract"
    __table_args__ = (
        sa.CheckConstraint("id = 1", name="single_row"),
        sa.CheckConstraint("minimum_contract >= 0", name="minimum_contract"),
        sa.CheckConstraint("phase in ('blocked', 'verified')", name="phase"),
        sa.CheckConstraint(
            "phase <> 'verified' OR (minimum_contract >= 1 AND epoch IS NOT NULL AND attested_at IS NOT NULL)",
            name="verified",
        ),
    )
    id: Mapped[int] = mapped_column(sa.SmallInteger, primary_key=True)
    minimum_contract: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    phase: Mapped[str] = mapped_column(sa.String(16), server_default="blocked")
    epoch: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"))
    attested_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class HandoffReceiver(Base):
    """Serviço autorizado a resolver encaminhamentos de um formulário e revisão."""

    __tablename__ = "handoff_receivers"
    __table_args__ = (
        sa.UniqueConstraint("id", "form_slug", name="uq_handoff_receivers_id_form"),
        sa.CheckConstraint("purpose = 'support'", name="purpose"),
        sa.CheckConstraint("projection = 'correlation.v1'", name="projection"),
        sa.CheckConstraint("reference_ttl_seconds BETWEEN 60 AND 604800", name="reference_ttl"),
        sa.CheckConstraint("revision >= 1 AND version >= 1", name="versions"),
    )
    id: Mapped[uuid.UUID] = _pk()
    form_slug: Mapped[str] = mapped_column(sa.ForeignKey("forms.slug"), unique=True)
    name: Mapped[str] = mapped_column(sa.String(100))
    entry_url: Mapped[str] = mapped_column(sa.Text)
    purpose: Mapped[str] = mapped_column(sa.String(16), default="support", server_default="support")
    projection: Mapped[str] = mapped_column(
        sa.String(32), default="correlation.v1", server_default="correlation.v1"
    )
    reference_ttl_seconds: Mapped[int] = mapped_column(sa.Integer, default=604800)
    revision: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=False, server_default=sa.false())
    created_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_by: Mapped[str] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class IntegrationCredential(Base):
    """Credencial de serviço: somente hash, escopo fixo e emissão sem recuperação de segredo."""

    __tablename__ = "integration_credentials"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["receiver_id", "form_slug"],
            ["handoff_receivers.id", "handoff_receivers.form_slug"],
            name="fk_integration_credentials_receiver_form",
        ),
        sa.UniqueConstraint(
            "created_by", "operation", "idempotency_hash", name="uq_integration_credentials_command"
        ),
        sa.CheckConstraint("receiver_revision >= 1 AND version >= 1", name="versions"),
        sa.CheckConstraint("purpose = 'support'", name="purpose"),
        sa.CheckConstraint("scope = 'handoffs:resolve'", name="scope"),
        sa.CheckConstraint("projection = 'correlation.v1'", name="projection"),
        sa.CheckConstraint("operation in ('create', 'rotate')", name="operation"),
        sa.CheckConstraint("expires_at IS NULL OR expires_at > created_at", name="expiry"),
    )
    id: Mapped[uuid.UUID] = _pk()
    key_id: Mapped[str] = mapped_column(sa.String(32), unique=True)
    name: Mapped[str] = mapped_column(sa.String(100))
    secret_hash: Mapped[str] = mapped_column(sa.String(64))
    receiver_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, index=True)
    receiver_revision: Mapped[int] = mapped_column(sa.Integer)
    form_slug: Mapped[str] = mapped_column(sa.String(64))
    purpose: Mapped[str] = mapped_column(sa.String(16), default="support", server_default="support")
    scope: Mapped[str] = mapped_column(
        sa.String(32), default="handoffs:resolve", server_default="handoffs:resolve"
    )
    projection: Mapped[str] = mapped_column(
        sa.String(32), default="correlation.v1", server_default="correlation.v1"
    )
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    created_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(sa.Text)
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    operation: Mapped[str] = mapped_column(sa.String(16))
    idempotency_hash: Mapped[str] = mapped_column(sa.String(64))
    command_fingerprint: Mapped[str] = mapped_column(sa.String(64))
    successor_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("integration_credentials.id")
    )


class HandoffReference(Base):
    """Intenção histórica por submissão; a referência recuperável fica somente cifrada."""

    __tablename__ = "handoff_references"
    __table_args__ = (
        sa.UniqueConstraint(
            "submission_id", "purpose", name="uq_handoff_references_submission_purpose"
        ),
        sa.CheckConstraint("purpose = 'support'", name="purpose"),
        sa.CheckConstraint("projection = 'correlation.v1'", name="projection"),
        sa.CheckConstraint("receiver_revision >= 1 AND version >= 1", name="versions"),
        sa.CheckConstraint("expires_at > created_at", name="expiry"),
        sa.CheckConstraint(
            "(issuance_state = 'ready' AND token_hash IS NOT NULL AND unavailable_code IS NULL) OR "
            "(issuance_state = 'unavailable' AND token_hash IS NULL AND token_ciphertext IS NULL "
            "AND unavailable_code IS NOT NULL)",
            name="issuance",
        ),
        sa.CheckConstraint("revoked_at IS NULL OR token_ciphertext IS NULL", name="revocation"),
    )
    id: Mapped[uuid.UUID] = _pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(sa.String(16), default="support", server_default="support")
    receiver_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("handoff_receivers.id"), index=True
    )
    receiver_revision: Mapped[int] = mapped_column(sa.Integer)
    entry_url: Mapped[str] = mapped_column(sa.Text)
    projection: Mapped[str] = mapped_column(
        sa.String(32), default="correlation.v1", server_default="correlation.v1"
    )
    token_hash: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    token_ciphertext: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(sa.Text)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    issuance_state: Mapped[str] = mapped_column(sa.String(16))
    unavailable_code: Mapped[str | None] = mapped_column(sa.String(64))


class PlanRelease(Base):
    """Autorização imutável de uma tentativa; decisão negativa de notificar também é um fato."""

    __tablename__ = "plan_releases"
    __table_args__ = (
        sa.UniqueConstraint("job_id", "attempt", name="uq_plan_releases_job_attempt"),
        sa.UniqueConstraint("job_id", "idempotency_key", name="uq_plan_releases_job_idempotency"),
        sa.CheckConstraint(
            "attempt >= 1 AND version = 1 AND form_version_number >= 1", name="versions"
        ),
        sa.CheckConstraint("mode in ('standard', 'alternative')", name="mode"),
        sa.CheckConstraint(
            "reason_code in ('operator_review', 'participant_request', 'alternative_channel', 'other')",
            name="reason_code",
        ),
        sa.CheckConstraint(
            "notification_decision in ('outbox', 'no_participant_channel', 'event_not_selected')",
            name="notification_decision",
        ),
    )
    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("jobs.id"), index=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("artifacts.id"))
    attempt: Mapped[int] = mapped_column(sa.Integer)
    form_slug: Mapped[str] = mapped_column(sa.String(64))
    form_version_number: Mapped[int] = mapped_column(sa.Integer)
    form_version: Mapped[str] = mapped_column(sa.String(64))
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    actor: Mapped[str] = mapped_column(sa.Text)
    occurred_at: Mapped[datetime] = _created_at()
    mode: Mapped[str] = mapped_column(sa.String(16))
    reason_code: Mapped[str] = mapped_column(sa.String(32))
    idempotency_key: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    command_fingerprint: Mapped[str] = mapped_column(sa.String(64))
    notification_decision: Mapped[str] = mapped_column(sa.String(32))


class EventSource(Base):
    """Autoridade histórica explícita para declarações externas de um diagnóstico."""

    __tablename__ = "event_sources"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["receiver_id", "form_slug"], ["handoff_receivers.id", "handoff_receivers.form_slug"]
        ),
        sa.UniqueConstraint("receiver_id", "receiver_revision", "namespace"),
        sa.UniqueConstraint("created_by", "idempotency_hash"),
        sa.CheckConstraint(
            "revision >= 1 AND version >= 1 AND receiver_revision >= 1", name="versions"
        ),
        sa.CheckConstraint("purpose = 'support'", name="purpose"),
    )
    id: Mapped[uuid.UUID] = _pk()
    receiver_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    receiver_revision: Mapped[int] = mapped_column(sa.Integer)
    form_slug: Mapped[str] = mapped_column(sa.String(64))
    purpose: Mapped[str] = mapped_column(sa.String(16), default="support", server_default="support")
    namespace: Mapped[str] = mapped_column(sa.String(64))
    name: Mapped[str] = mapped_column(sa.String(100))
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(sa.Boolean, default=False, server_default=sa.false())
    created_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_by: Mapped[str] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    idempotency_hash: Mapped[str] = mapped_column(sa.String(64))
    command_fingerprint: Mapped[str] = mapped_column(sa.String(64))


class EventCredential(Base):
    """Segredo de eventos independente das credenciais de resolução e administração."""

    __tablename__ = "event_credentials"
    __table_args__ = (
        sa.UniqueConstraint("created_by", "operation", "idempotency_hash"),
        sa.CheckConstraint("scope = 'journey:events:write'", name="scope"),
        sa.CheckConstraint("source_revision >= 1 AND version >= 1", name="versions"),
        sa.CheckConstraint("operation in ('create', 'rotate')", name="operation"),
        sa.CheckConstraint("expires_at IS NULL OR expires_at > created_at", name="expiry"),
    )
    id: Mapped[uuid.UUID] = _pk()
    source_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("event_sources.id"), index=True)
    source_revision: Mapped[int] = mapped_column(sa.Integer)
    scope: Mapped[str] = mapped_column(
        sa.String(32), default="journey:events:write", server_default="journey:events:write"
    )
    key_id: Mapped[str] = mapped_column(sa.String(32), unique=True)
    secret_hash: Mapped[str] = mapped_column(sa.String(64))
    name: Mapped[str] = mapped_column(sa.String(100))
    version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1")
    created_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(sa.Text)
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    operation: Mapped[str] = mapped_column(sa.String(16))
    idempotency_hash: Mapped[str] = mapped_column(sa.String(64))
    command_fingerprint: Mapped[str] = mapped_column(sa.String(64))
    successor_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("event_credentials.id"))


class JourneyEvent(Base):
    """Snapshot declarado; o apagamento remove o conteúdo protegido sem cópia recuperável."""

    __tablename__ = "journey_events"
    __table_args__ = (
        sa.UniqueConstraint("source_id", "event_id_digest", name="uq_journey_events_event"),
        sa.UniqueConstraint(
            "source_id",
            "reference_id",
            "entity_kind",
            "entity_id_digest",
            "entity_revision",
            name="uq_journey_events_revision",
        ),
        sa.CheckConstraint("entity_revision >= 1 AND source_revision >= 1", name="revisions"),
        sa.Index("ix_journey_events_history", "reference_id", "received_at", "id"),
    )
    id: Mapped[uuid.UUID] = _pk()
    source_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("event_sources.id"))
    credential_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("event_credentials.id"))
    source_revision: Mapped[int] = mapped_column(sa.Integer)
    reference_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("handoff_references.id", ondelete="CASCADE")
    )
    event_id_digest: Mapped[str] = mapped_column(sa.String(64))
    entity_id_digest: Mapped[str] = mapped_column(sa.String(64))
    fingerprint: Mapped[str] = mapped_column(sa.String(64))
    type: Mapped[str] = mapped_column(sa.String(48))
    entity_kind: Mapped[str] = mapped_column(sa.String(16))
    entity_revision: Mapped[int] = mapped_column(sa.Integer)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    received_at: Mapped[datetime] = _created_at()
    safe_data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    protected_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, deferred=True
    )
    erased_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class PartialCapture(Base):
    """Respostas ainda não submetidas; nenhuma relação com Job ou Lead."""

    __tablename__ = "partial_captures"
    id: Mapped[uuid.UUID] = _pk()
    form_slug: Mapped[str] = mapped_column(sa.String(64), index=True)
    form_version_number: Mapped[int] = mapped_column(sa.Integer)
    create_key_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    sealed_token: Mapped[str] = mapped_column(sa.Text)
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB)
    consent: Mapped[dict[str, Any]] = mapped_column(JSONB)
    tracking: Mapped[dict[str, Any]] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(sa.Integer, default=1)
    step: Mapped[int] = mapped_column(sa.Integer)
    last_request_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    last_digest: Mapped[str] = mapped_column(sa.String(64))
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("submissions.id", ondelete="CASCADE"), unique=True
    )
    created_at: Mapped[datetime] = _created_at()
    last_activity_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)


class PartialDelivery(Base):
    """Fato interno fixado com a captura; transporte separado e repetível."""

    __tablename__ = "partial_deliveries"
    __table_args__ = (sa.UniqueConstraint("partial_id", "event"),)
    id: Mapped[uuid.UUID] = _pk()
    partial_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("partial_captures.id", ondelete="CASCADE")
    )
    destination_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    destination_revision: Mapped[int] = mapped_column(sa.Integer)
    event: Mapped[str] = mapped_column(sa.String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0)
    next_attempt_at: Mapped[datetime] = _created_at()
