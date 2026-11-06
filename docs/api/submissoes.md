# Contexto exato de uma submissão

`GET /admin/api/v1/submissions/{submission_id}/commercial-context?form=<slug>` consulta uma
submissão salva, com ou sem PDF. No MCP, use `get_submission_commercial_context` com os mesmos
parâmetros. A operação não exige recibo de captura e não busca por nome, e-mail ou telefone.
O UUID recebido em `submissao.persistida` v2 pode ser usado diretamente, sem alterar esse evento.

A chave administrativa conserva sua autoridade atual. `form` é obrigatório e valida o formulário
esperado; não é escopo da credencial. Submissão inexistente ou formulário divergente produz
`404 not_found`; ausência de autenticação, `401`. UUID malformado ou parâmetro ausente produz `422`.

A resposta informa `submission_id`, `form_slug`, `form_version`, `form_version_number`,
`created_at`, `historical_version_available` e `generation` (`none` ou `pdf`). Sem a versão
histórica, `generation` é nulo e `historical_version_available` é falso; não usamos a versão
atual como substituta. `job: null` significa ausência de pedido, inclusive em uma submissão PDF.
Isso não impede a integração de processar as respostas já persistidas.

Quando existe, `job` contém `id`, `public_id`, `attempt`, `status`, `current_pdf` e `release`.
A correlação é exclusivamente `Submission.id` → `Job.submission_id`, que é único no banco.
Reprocessar incrementa a tentativa do mesmo pedido; nenhuma busca por data escolhe um pedido
"mais recente". Vínculo ambíguo, tentativa inválida ou pedido associado a uma versão de captura
produz `409 state_conflict`, sem escolher arbitrariamente nem corrigir dados durante a leitura.
A leitura mantém o pedido estável enquanto consulta sua tentativa, PDF e liberação.

`current_pdf` usa o contrato de disponibilidade existente, mas esta consulta **não acessa storage**:
sem artefato da tentativa atual, `state=missing`; com remoção registrada, `state=removed`;
com artefato registrado, `state=unknown`. Um `verified_at` histórico não comprova acesso atual,
portanto `available` permanece falso sem prova atual. `unknown` não significa PDF inexistente.
Para comprovar acessibilidade atual, a consulta existente `get_job` faz a verificação de storage.

`release` usa a política histórica e o fato da tentativa atual: `automatic`, `held`, `released`
ou `unavailable`, sem supor que autorização implica disponibilidade. Depois de reprocessar,
PDF e liberação de uma tentativa antiga não valem para a nova. Nenhum token, URL participante,
URL assinada, chave de storage, documento ou capacidade de download sai nesta resposta.
A leitura não gera, libera, notifica nem chama provedores externos.

`lead_data` vem nulo por padrão, mesmo para `total`. Somente `include_lead_data=true` com papel
`total` retorna `lead_data.tracking`, exatamente como persistido na submissão; tracking vazio
persistido continua `{}`.
Se a retenção apagar a submissão antes da leitura autorizada de tracking, a consulta retorna
`404 not_found`; não transforma a exclusão em tracking vazio.
Papel `leitura` com essa opção recebe `403 forbidden_role`. Respostas e identidade não fazem parte
deste contrato mínimo. No MCP, a opção também depende de `GERADOR_MCP_LEAD_DATA=1`; sem ela o
argumento não é exposto e o cliente impede sua inclusão. Considere que esse conteúdo chega ao
provedor do modelo quando a opção está habilitada.

Esta consulta não cria credencial de serviço com privilégio mínimo, não implementa CRM ou agenda
e não condiciona a entrega das respostas ao PDF. Consulte também [Captura](captura.md),
[Integrações](integracoes.md) e o [OpenAPI](openapi.json).
