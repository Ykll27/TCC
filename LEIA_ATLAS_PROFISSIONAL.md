# Atlas — versão profissional de UI/UX e fluxo principal

Esta versão reorganiza o Atlas para seguir o padrão visual da referência enviada: dashboard SaaS claro, maduro, com sidebar branca arredondada, topbar com busca, cards limpos, azul escuro como cor principal e menos poluição visual.

## O que foi melhorado

### Visual e usabilidade
- Novo `base.html` com sidebar fixa profissional.
- Nova paleta no `static/css/atlas.css`.
- Tipografia moderna, cards padronizados, botões discretos e badges suaves.
- Topbar com busca global da tela.
- Menu separado por categorias: Organização, Avaliações, Ferramentas e Sistema.
- Remoção do excesso de aparência infantil: menos emojis, mais ícones técnicos via Bootstrap Icons.

### Funções mais usadas
- Painel inicial refeito para o fluxo principal: criar prova, cadastrar aluno rápido, corrigir e consultar resultados.
- Nova página `/estrutura` para módulos, disciplinas e turmas.
- Nova página `/alunos` com cadastro, importação CSV e busca.
- Nova página `/avaliacoes` com ações rápidas: abrir, gerar folhas e relatório.
- Nova página `/gabaritos` para conferir gabaritos oficiais.
- Nova página `/folhas` para localizar folhas personalizadas.
- Nova página `/relatorios` para acessar relatórios por avaliação.

### Segurança da correção
- A correção por upload foi ajustada para não usar mais a prova base como fallback de aluno.
- Se o QR falhar ou estiver inconsistente, o Atlas não salva nota automaticamente.
- Folhas sem QR confiável entram em Identificação Assistida.
- Isso evita o erro de lançar nota no aluno errado.

### Scan por câmera
- Mantém o Scan com QR obrigatório, loop adaptativo e proteção contra duplicidade.
- Interface geral recebeu o novo padrão visual automaticamente pela base e CSS.

## Como subir no GitHub

```bash
cd ~/Downloads
unzip atlas_profissional_funcoes_usadas.zip
cd atlas

git init
git branch -M main
git remote add origin https://github.com/Ykll27/TCC.git
git add .
git commit -m "Reformula Atlas com UI profissional e melhora funcoes principais"
git push -u origin main --force
```

Depois no Render:

```text
Manual Deploy → Deploy latest commit
```

## Observação

Use PostgreSQL persistente no Render com `DATABASE_URL`. Se o `DATABASE_URL` não estiver configurado, o sistema cai para SQLite local, que é apenas para teste.
