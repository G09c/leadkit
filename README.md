# Leadkit

Formulários de diagnóstico que produzem planos de ação e PDFs. A operadora edita perguntas,
condições, pontuação, calendário, instruções e templates no admin; publica versões imutáveis e
acompanha os pedidos pela interface, API ou MCP.

O motor usa Python 3.11, FastAPI, PostgreSQL 16, Hatchet, OpenAI e WeasyPrint. PDF real e
armazenamento local funcionam com conteúdo fictício; o processamento pela fila exige um Hatchet
configurado e um worker conectado.

Para operar formulários, respostas e integrações, consulte o [guia da operadora](docs/guia-da-operadora/README.md).

## Instalação

Instale Python 3.11, uv, PostgreSQL 16 e as bibliotecas nativas do WeasyPrint (Pango e HarfBuzz).
No macOS, `brew install postgresql@16 pango harfbuzz` e, se necessário,
`export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`.

```sh
git clone https://github.com/LevyDeSales/leadkit.git
cd leadkit
export UV_PROJECT_ENVIRONMENT=../leadkit-venv
uv sync --locked --extra dev --extra mcp
cp app/.env.example .env
```

Configure o banco próprio e os segredos da instalação no `.env` (nunca versione esse arquivo).
O Alembic lê `DATABASE_URL` do ambiente, então exporte a variável antes de migrar:

```sh
export DATABASE_URL=postgresql+psycopg://leadkit@127.0.0.1:5432/leadkit
uv run alembic -c app/alembic.ini upgrade head
uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Configure a administradora conforme [app/README.md](app/README.md). Importe
[examples/demo.json](examples/demo.json) no admin, teste o rascunho, publique-o e abra `/p/demo`.
O demo é fictício e não é semeado automaticamente no banco. Para processar envios, execute também
`uv run python -m app.worker` com um Hatchet próprio acessível. `LLM_MODE=fake`, `PDF_MODE=weasyprint`
e `STORAGE_MODE=local` permitem ensaiar geração e PDF sem OpenAI/S3; o modo fake não substitui a fila.

## Personalização

Crie formulários pelo admin ou importe uma definição de formato 1. A marca padrão é textual e usa
fontes livres. A [configuração de marca](docs/branding.md) permite um overlay privado somente
leitura, sem copiar logos da instalação para o repositório ou para o formulário exportado.

A atualização e a futura adoção por uma instalação existente seguem o
[roteiro de preservação e migração](docs/migrations.md).

Consulte [documentação técnica](app/README.md), [contribuição](CONTRIBUTING.md),
[segurança](SECURITY.md) e [índice](docs/README.md). Código próprio sob [MIT](LICENSE);
dependências e fontes conservam suas licenças e avisos próprios.

A [captura parcial e retomada](docs/partial-capture.md) é opt-in por versão e documenta os contratos de API e eventos internos.
