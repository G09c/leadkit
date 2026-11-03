# Captura parcial e retomada

Captura parcial é opt-in por versão publicada. Omitir `partial_capture` conserva o envio completo
anterior. A operadora pode editar a definição no editor JSON, pela API de rascunhos ou pelo MCP;
as três superfícies usam o mesmo compilador e publicam outra versão imutável.

```json
{
  "partial_capture": {
    "contact_permission_field": "contact_permission",
    "contact_permission_value": "yes",
    "ttl_hours": 72
  }
}
```

A identidade deve estar na primeira etapa. A autorização é uma pergunta `choice` obrigatória e
incondicional dessa etapa; o valor autorizado precisa ser uma opção existente. Processamento usa
`consent.acquisition_method=checkbox` e `consent.position=first_step`. Autorizar processamento
não autoriza contato. Quem recusa contato continua respondendo e pode concluir normalmente.

O prazo fica entre 1 e 168 horas, limitado pela retenção do formulário no momento da criação.
Salvar outra etapa atualiza `last_activity_at`, mas não renova o prazo. A participante vê confirmação somente após commit e pode
copiar o link de retomada. Quando o armazenamento local está disponível, o navegador guarda
a capacidade temporária para retomar no mesmo aparelho; nenhuma resposta pessoal entra nessa chave. O fragmento `#resume=` leva apenas capacidade aleatória; o navegador o
remove antes de enviar a capacidade no corpo de um POST. O link dá acesso às respostas salvas,
por isso deve ser compartilhado somente pela própria participante ou pelo receptor autorizado.
Sem JavaScript, o formulário continua permitindo envio completo; salvamento por etapas exige JS.

## API pública

`POST /v1/forms/{slug}/partials` recebe `request_id` UUID aleatório, `step` começando em zero,
`answers` das etapas preenchidas, `consent` e `tracking` opcional. A criação exige etapa zero.
Para atualizar, acrescente `token` e a `revision` devolvida pelo último salvamento.
A resposta 201 contém `id`, `token`, `revision`, `step`, `version_number`, `expires_at`, respostas,
consentimento e origem. Replay exato da última operação devolve 200 sem repetir efeitos.
Outra operação sobre revisão antiga recebe 409; não sobrescreve as respostas silenciosamente.
Voltar uma etapa conserva respostas posteriores ainda válidas; condições que ocultem uma pergunta
removem sua resposta. Até a etapa atual, perguntas condicionais visíveis continuam obrigatórias
e opções ocultadas por condição são recusadas, pelas mesmas regras do envio completo. Ao voltar,
perguntas reveladas em etapas posteriores só serão exigidas quando essas etapas forem visitadas;
opções posteriores que perderem a condição são removidas para nova resposta. Cada salvamento registra a declaração atual de consentimento, inclusive
retirada de marketing por e-mail ou WhatsApp, também refletida na retomada HTML.

Mudar identidade, autorização de contato ou declaração de consentimento cria uma captura sucessora
com outro `id` e `token`, preservando respostas válidas, versão, origem e vencimento original.
A cliente deve sempre guardar o token/revisão retornados. O token antigo é invalidado na mesma
transação. Repetir exatamente a operação de sucessão com seu request_id e token originais recupera
a mesma sucessora enquanto ela permanece disponível; nunca cria uma terceira captura.

`POST /v1/forms/{slug}/partials/resume` recebe somente `token` e devolve o snapshot histórico.
`POST /p/{slug}/retomar` troca o token pelo HTML da versão respondida. Capacidade ausente,
inválida, expirada ou já concluída recebe a mesma recusa 404. Nenhuma busca usa e-mail ou telefone.

A API de submissões existente aceita `partial_token` opcional. Nesse caso, valida todas as respostas
na versão da captura e confirma uma única submissão correlacionada, na mesma transação.
O replay final conserva a submissão. Somente a submissão completa pode seguir a geração configurada;
salvar etapas nunca cria Job, chama LLM ou gera PDF.

As rotas de capacidade compartilham limite de 30 requisições por minuto e cliente, por processo,
com rajada de 30 e `Retry-After`. Assim como o limitador administrativo existente, reinício limpa
os baldes e mais réplicas multiplicam o limite. O proxy precisa preservar a identificação correta
da cliente. O corpo da API parcial tem teto de 32 KiB e erros não reproduzem os valores recebidos.

## Eventos ao receptor interno

Configure um destino específico do formulário, com finalidade `internal`, selecionando juntos
`contato.capturado`, `contato.revogado` e `formulario.concluido`. O destino global não é herdado. Sem essa autorização,
as respostas podem ser salvas, mas nenhuma entrega é criada. Habilitar um destino depois não
reenvia capturas anteriores. A conclusão conserva o receptor e a revisão fixados no contato. Quando não houve evento de
contato, a conclusão fixa o destino específico atual, inclusive para autorização de contato recusada.

O transporte existente envia JSON com `Idempotency-Key: <event_id>`, `X-MCC-Event`,
`X-MCC-Timestamp` em segundos Unix e `X-MCC-Signature: v1=<hex>`, calculada por
HMAC-SHA256 do segredo sobre `timestamp + '.' + corpo`. Cada tentativa é assinada novamente.
2xx confirma transporte; 408, 425, 429 e 5xx permitem retry; demais respostas encerram a tentativa.
Redirecionamentos não são seguidos. O receptor deve deduplicar por `event_id` e correlacionar por
`issuer` + `partial.id`, inclusive se receber conclusão antes do contato.

```json
{
  "schema_version": 1,
  "event": "contato.capturado",
  "event_id": "00000000-0000-4000-8000-000000000001",
  "issuer": "leadkit:00000000-0000-4000-8000-000000000002",
  "occurred_at": "2026-01-01T12:00:00+00:00",
  "form": {"slug": "pesquisa-exemplo", "version": "pesquisa-exemplo-v1", "version_number": 1},
  "partial": {"id": "00000000-0000-4000-8000-000000000003", "expires_at": "2026-01-04T12:00:00+00:00"},
  "submission": null,
  "contact": {"name": "Participante de exemplo", "email": "pessoa@example.test", "phone": null},
  "consent": {
    "processing": {"evidence_schema": 2, "method": "checkbox", "action": "confirm", "source": "public_api_declaration", "privacy_at": "2026-01-01T12:00:00+00:00", "text_version": "pesquisa-exemplo-v1", "marketing_email": false, "marketing_whatsapp": false},
    "contact": {"field": "contact_permission", "value": "yes", "authorized": true}
  },
  "tracking": {"utm_source": "exemplo"},
  "resume_url": "https://forms.example.test/p/pesquisa-exemplo#resume=CAPACIDADE_OPACA"
}
```

Na conclusão, `event` é `formulario.concluido`, `submission` contém `id` e `submitted_at`, e
`resume_url` fica ausente. Quando há geração de PDF, `job` contém `public_id` e `attempt`, sem
capacidade de acesso; a consulta administrativa existente usa esse `public_id`. A conclusão
continua elegível após expirar o link, até a retenção da submissão. O envio completo sem token
(inclusive sem JS ou com uma etapa) também produz esse evento, uma única vez por submissão
idempotente. Nesse caso `partial.id` é uma correlação técnica criada na conclusão, sem capacidade
de retomada, e `partial.expires_at` coincide com a conclusão.

A revogação durante um salvamento emite `contato.revogado` uma vez, com o mesmo `partial.id`,
`submission=null`, `authorized=false` e sem `resume_url`. O receptor deve cancelar lembretes
mesmo se receber a revogação antes do contato, e não reabrir a autorização por evento atrasado. O consentimento final pode indicar `authorized=false`; o receptor ainda
deve encerrar lembretes pela correlação. Na sucessão, a captura antiga emite revogação somente se tinha uma intenção de contato;
intenções antigas pendentes são canceladas, inclusive retries. Payloads de eventos já tentados
não são alterados. A sucessora autorizada emite contato com outro `partial.id` e outro `event_id`;
recusa→autorização sem contato anterior não emite revogação. `occurred_at` do novo contato é a
criação da sucessora; a revogação usa seu instante próprio. Esses horários não substituem os IDs
nem exigem ordem de transporte. O receptor conserva o tombstone **da captura revogada**, sem
reabrir esse ID quando chegar um contato atrasado; não deve interpretar a sucessão como permissão
para limpar opt-out global, respostas ou atendimento existentes da pessoa.

Contato pendente não é enviado depois da conclusão,
expiração ou revogação observada na captura. Alterar o receptor bloqueia entregas antigas em vez
de redirecioná-las. Transporte confirmado não comprova mensagem ou atendimento externo.

A outbox e as respostas são gravadas juntas. O reconciliador existente despacha fora da requisição
HTTP, com backoff persistido. Em DEV sem `RECONCILE_CRON`, a operadora executa o reconciliador
explicitamente; configurar o recurso não inicia um serviço nem habilita um cron.

## Retenção e migração

A migration `0021_partial_capture` adiciona tabelas sem modificar versões publicadas ou contratos
anteriores. O guard estrutural conserva inventário PostgreSQL 16 específico da revisão.
O cron de retenção apaga respostas, consentimentos, origem, capacidade cifrada e payloads de
capturas incompletas expiradas; capturas concluídas acompanham a retenção da submissão.
Conserva somente o tombstone técnico da captura, incluindo hashes, para recusar replay tardio de
uma criação já expirada. Antes de apagar a submissão concluída (com ou sem PDF), a retenção desassocia a captura,
apaga conteúdo/capacidade e entregas e preserva o tombstone técnico, na mesma transação.
Assim o CASCADE da FK não apaga a barreira de replay. A retenção da submissão conserva seus prazos anteriores.
Cópias entregues ao receptor seguem a política dele.

Este recurso não configura CRM, envia mensagens ou publica formulários. Deploy e habilitação de
uma instalação são ações separadas dos testes sintéticos e da entrega do código.
