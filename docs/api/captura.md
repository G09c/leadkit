# Formulários de captura

## Enviar respostas ao concluir o formulário

No destino próprio do formulário, escolha finalidade **Interna** e **Enviar respostas ao concluir
o formulário**. Com ou sem PDF, as respostas são enviadas depois de salvar, inclusive se o PDF
estiver pendente, falhar ou aguardar liberação. Essa escolha exige atualização compatível concluída;
não funciona no destino padrão e não envia respostas anteriores. A seleção legada de captura e
a nova seleção são alternativas: escolha somente uma para o mesmo destino.

A consulta de captura conserva `deliveries`; no pedido PDF, `get_job.persisted_deliveries` mostra
a sincronização das respostas. O painel, a API e o MCP consultam o mesmo estado seguro: pendente,
confirmado ou falho, com tentativas, revisão original/atual e código de erro. Respostas salvas
continuam salvas quando o transporte falha. Sucesso confirma o transporte, sem comprovar leitura.
O retry exige confirmação, epoch e revisão observados e conserva o mesmo evento, conteúdo, destino
e revisão originais. Uma configuração alterada exige nova decisão para os próximos envios; não
redireciona intenções anteriores. Não use reprocessamento do PDF para repetir respostas.

O contrato técnico e a prova de recebimento estão em [Integrações](integracoes.md).
Para enriquecer um evento pelo UUID da submissão, com ou sem PDF, consulte
[Contexto exato de uma submissão](submissoes.md).

Ao criar um formulário, escolha **Somente salvar respostas**. A finalidade fica em `generation`
na versão publicada: `none` captura; `pdf` ou ausência conserva a geração de plano e PDF.
O [exemplo sintético](../../examples/capture.json) pode ser importado pelas mesmas superfícies.

Na captura, você preenche perguntas, identidade, consentimento e respostas de exemplo; não precisa
de instruções, prompt, modelo/exemplo de plano ou templates. A conclusão deve ser confirmação,
botão ou encaminhamento. Resultado PDF é recusado em `completion.mode`.
O teste local valida respostas, data, pontuação e conclusão sem gravar uma submissão ou gastar IA.
No editor, salvar como captura remove a configuração de geração do rascunho. Voltar explicitamente
para PDF inicia os blocos de geração pelo modelo inicial, preservando perguntas e respostas de
exemplo. Você pode editar essa configuração antes de testar e publicar; versões publicadas anteriores
continuam disponíveis no histórico. API/MCP podem salvar o documento PDF completo explicitamente.
Publicação e simulação não renderizam PDF nem exigem teste pago; operações de geração são
inaplicáveis e retornam `409 state_conflict` na API e no MCP.

O envio HTML conserva consentimento, honeypot, limites e chave de repetição. A API pública continua
em `POST /v1/forms/{slug}/submissions`, com `202` após commit. O ramo PDF mantém seu contrato.
Captura retorna `kind=capture`, `status=captured`, `submission_id`, `receipt_token`, `receipt_url`,
`replay` e `completion`; não retorna estado, token ou links de PDF. Reenvio com a mesma chave usa
sempre a versão e a conclusão originais; respostas divergentes retornam conflito.

O recibo em `/s/{public_id}/conclusao` troca seu token por cookie HttpOnly restrito ao recibo.
Ele mostra somente o próximo passo histórico; nunca respostas ou contato. Link e cookie não abrem
PDF. Mensagem e botão são editáveis, escapados como texto, e o destino é usado exatamente como
configurado, sem acrescentar token, referência ou dados da participante.

Você consulta capturas no painel **Capturas**, ou por `list_captures`/`get_capture` na API/MCP.
O papel `leitura` recebe estado e versão. Dados históricos exigem `total` e
`include_lead_data=true`; o MCP também exige `GERADOR_MCP_LEAD_DATA=1`.
A lista pagina por cursor e filtra formulário e número da versão, sem busca pessoal.

A retenção usa os prazos existentes por formulário, contados da chegada da submissão. Replay e
validade do recibo não prolongam o prazo. Prévia, confirmação de redução e auditoria distinguem
capturas de pedidos e arquivos PDF. A contagem `seen` aceita `pedidos:PDFs:capturas`; a forma antiga
`pedidos:PDFs` equivale a zero capturas e não confirma uma redução que já apague capturas.
A exclusão remove recibo e respostas e conserva a lead enquanto houver outra submissão válida.

Após o commit da captura, o sistema agenda a entrega assíncrona das respostas às integrações
aplicáveis e ativas, pelo evento `submissao.persistida`. A intenção fica persistida com o destino e a revisão
autorizados, e o reconciliador recupera falhas de disparo. Falha ou timeout do transporte conserva
a captura confirmada e sua conclusão; a entrega fica pendente ou com falha para consulta e
repetição. Esse fluxo funciona sem IA, plano ou PDF. Reenvio da mesma
submissão conserva as intenções originais, e ativar uma conexão depois não faz envio retroativo.

Prepare o perfil de captura no Baserow com `submission.id` como chave; a ativação da conexão segue
a finalidade publicada. No webhook, declare `submissao.persistida` entre os eventos aceitos.
A publicação aponta configurações incompatíveis localmente. Você acompanha as entregas na captura
e na integração, inclusive por API/MCP; **Repetir entrega** exige papel total, confirmação e versões
esperadas do mesmo destino. Sucesso indica confirmação do transporte; leitura pela participante
exige evidência própria. Captura usa evento próprio, distinto de `plano.concluido`.

Consulte a [matriz das operações de captura](cobertura.md#capturas-sem-geração), o
[contrato OpenAPI](openapi.json) e a [operação das integrações](../../app/README.md#integrações-de-captura)
para configuração, repetição e limites. O [roteiro de migração e rollback](../migrations.md#entregas-de-captura-e-rollback)
preserva as capturas e seus registros confirmados.

A finalidade do receptor de captura é sempre **interna**: configure `purpose=internal` no webhook
e na conexão Baserow. Finalidade participante é recusada com orientação para corrigir o destino
efetivo, inclusive o padrão herdado. Não há projeção pública de respostas de captura; veja
[planos por audiência](projecoes.md).
