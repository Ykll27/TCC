# Atlas - Ferramentas do TCC adicionadas

Esta versão adiciona um conjunto de ferramentas auxiliares no menu de três risquinhos do Atlas.

## Funções adicionadas

- **Anotações:** registros livres para observações pedagógicas, bugs, ideias e decisões do projeto.
- **Diário de bordo:** histórico do desenvolvimento, com data, atividade, responsável, status e observações.
- **Checklist do TCC:** lista padrão com partes da documentação e apresentação, com progresso em porcentagem.
- **Cronograma / Kanban:** tarefas organizadas por A fazer, Em andamento, Concluído e Travado.
- **Central de testes:** valida banco, OpenCV, PDF, QR Code, IA configurada e câmera do navegador.
- **Sobre o Atlas:** página de apresentação do sistema, problema resolvido, tecnologias e diferenciais.

## Arquivos alterados

- `app.py`: novas rotas e lógica das ferramentas.
- `bd.py`: novas tabelas e migrações seguras.
- `templates/base.html`: menu lateral/offcanvas dos três risquinhos.
- `static/css/atlas.css`: pequenos ajustes visuais.

## Templates adicionados

- `templates/ferramentas.html`
- `templates/anotacoes.html`
- `templates/diario_bordo.html`
- `templates/checklist_tcc.html`
- `templates/cronograma.html`
- `templates/central_testes.html`
- `templates/sobre_atlas.html`

## Como atualizar no Render

Depois de substituir os arquivos no repositório:

```bash
git add .
git commit -m "Adiciona ferramentas do TCC ao Atlas"
git push
```

No Render:

```text
Manual Deploy → Deploy latest commit
```

O banco será atualizado automaticamente pelo `iniciar_banco()` sem apagar dados antigos.
