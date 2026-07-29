# Atlas com banco persistente

O Atlas agora usa **PostgreSQL** quando a variável de ambiente `DATABASE_URL` existe.
Se `DATABASE_URL` não estiver configurada, ele continua usando SQLite local para testes no computador.

## Por que isso corrige o problema das contas sumindo?

No Render, o sistema de arquivos do web service pode ser reiniciado/recriado. Se o Atlas salva contas em `atlas.sqlite3` dentro do servidor, esse arquivo pode desaparecer em restart/redeploy. Usando PostgreSQL externo, as contas, alunos, avaliações e resultados ficam fora do servidor Flask.

## Variáveis no Render

Configure no Render:

```env
SECRET_KEY=sua_chave_grande
DATABASE_URL=postgresql://usuario:senha@host:porta/banco
GOOGLE_API_KEY=sua_chave_opcional
MODELO_GEMINI=gemini-2.0-flash
```

Pode usar Render Postgres, Supabase, Neon ou outro PostgreSQL.

## Como testar se está usando PostgreSQL

Depois do deploy, abra o shell/logs do Render. O Atlas não deve criar `atlas.sqlite3` como banco principal quando `DATABASE_URL` existir.

## Importante

Dados já perdidos do SQLite antigo não voltam automaticamente. A partir deste pacote, os novos cadastros ficam no PostgreSQL.
