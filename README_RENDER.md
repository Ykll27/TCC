# Atlas - Deploy no Render

Projeto Flask preparado para Render.

## Build Command

```bash
pip install -r requirements.txt
```

## Start Command

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180
```

## Variáveis de ambiente

Obrigatória:

```env
SECRET_KEY=uma-chave-grande-e-aleatoria
```

Opcionais para IA:

```env
GOOGLE_API_KEY=sua-chave-do-Gemini
MODELO_GEMINI=gemini-2.0-flash
```

O Atlas usa o mínimo possível de IA. Por padrão, ele tenta banco local/cache e geração reserva. A IA só é usada quando o professor permite no formulário ou quando uma leitura de gabarito fica pouco confiável no fluxo de upload.

## Câmera/Scan

O modo Scan desliga a câmera automaticamente ao finalizar a sessão, sair da página, recarregar ou ocultar a aba.

## Primeiro acesso

Ao abrir o sistema online, crie a conta do professor em `/cadastro`.

## Banco persistente

Configure um PostgreSQL externo e adicione a variável:

```env
DATABASE_URL=postgresql://usuario:senha@host:porta/banco
```

Sem `DATABASE_URL`, o Atlas usa SQLite local apenas para teste. No Render, não use SQLite local para contas reais.
