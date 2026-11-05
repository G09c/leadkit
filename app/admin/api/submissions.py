"""Consulta administrativa exata de submissões, também projetada para MCP."""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Query
from pydantic import AwareDatetime, BaseModel, Field

from app.admin.api.errors import ApiError
from app.admin.api.jobs import INCLUDE_LEAD_DATA, LEAD_DATA_403
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.jobs import CurrentPdf, JobStatus, ReleaseStatus
from app.admin.api.security import CurrentKey, ensure_role
from app.admin.queries import submissions
from app.deps import get_deps

router = OperationRouter(
    tag="Submissões", description="Contexto exato de respostas salvas, com ou sem geração de PDF."
)


class SubmissionJobContext(BaseModel):
    id: UUID
    public_id: str
    attempt: int
    status: JobStatus
    current_pdf: CurrentPdf = Field(
        description="Sem consulta ao storage: artefato ausente é missing, purgado é removed e registrado é unknown; available não é presumido."
    )
    release: ReleaseStatus


class SubmissionTracking(BaseModel):
    tracking: dict[str, Any]


class SubmissionCommercialContext(BaseModel):
    submission_id: UUID
    form_slug: str
    form_version: str
    form_version_number: int
    created_at: AwareDatetime
    historical_version_available: bool
    generation: Literal["pdf", "none"] | None = Field(
        description="Finalidade da versão respondida; nulo se a versão histórica está indisponível."
    )
    job: SubmissionJobContext | None = Field(
        default=None, description="Pedido ligado exclusivamente a esta submissão; nulo se ausente."
    )
    lead_data: SubmissionTracking | None = None


@router.operation(
    "GET",
    "/submissions/{submission_id}/commercial-context",
    operation_id="get_submission_commercial_context",
    role="leitura",
    lead_data=True,
    summary="Consultar contexto exato de submissão",
    description=(
        "Consulta por UUID da submissão e formulário esperado, nunca por contato. "
        "O parâmetro form valida o contexto; não restringe a autoridade da chave administrativa. "
        "Tracking só é lido em lead_data com papel total e include_lead_data=true. "
        "Não gera, libera ou entrega PDF, nem chama provedores externos."
    ),
    returns="Metadados históricos, pedido atual ou ausência explícita, sem capacidade de download.",
    response_model=SubmissionCommercialContext,
    errors={
        403: LEAD_DATA_403,
        404: "Submissão inexistente ou formulário divergente.",
        409: "state_conflict: vínculos ou tentativa incompatíveis; nenhum pedido é escolhido.",
    },
)
def get_submission_commercial_context(
    submission_id: UUID,
    form: Annotated[str, Query(min_length=1, max_length=64)],
    key: CurrentKey,
    include_lead_data: INCLUDE_LEAD_DATA = False,
) -> SubmissionCommercialContext:
    if include_lead_data:
        ensure_role(key, "total", what="Ler dados da participante")
    deps = get_deps()
    with deps.sessions() as session:
        try:
            found = submissions.commercial_context(
                session, deps.forms, submission_id, form=form, with_lead=include_lead_data
            )
        except submissions.ContextConflict:
            raise ApiError(
                "state_conflict", "O contexto da submissão está inconsistente."
            ) from None
    if found is None:
        raise ApiError("not_found", "Submissão não encontrada neste formulário.")
    return SubmissionCommercialContext(**found)
