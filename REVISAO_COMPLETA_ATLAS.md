# Revisão Completa do Projeto Atlas

## Diagnóstico geral

O Atlas já possui uma proposta forte para TCC: criar avaliações, gerar folhas com QR Code, corrigir por câmera/upload, registrar resultados e produzir relatórios. O sistema deixou de ser apenas um corretor de gabarito e virou uma plataforma de apoio escolar.

A revisão abaixo cobre design, experiência do usuário, backend, banco, visão computacional, segurança e funcionalidades.

---

## 1. Design e interface

### Pontos fortes

- Interface com Bootstrap 5.
- Cards arredondados e visual mais moderno.
- Menu lateral com ferramentas do TCC.
- Tela de Scan com timer, preview e últimas leituras.
- Separação visual entre criação, correção, resultados e ferramentas.

### Melhorias aplicadas

- A tela de Scan agora informa que o QR é obrigatório.
- A seleção da sessão foi renomeada para **Avaliação da sessão**, evitando confusão com aluno específico.
- Foi adicionada escolha de velocidade: rápida, equilibrada e econômica.
- Foram adicionados indicadores de segurança: QR obrigatório, sem fallback de aluno, loop adaptativo e câmera desligada ao sair.

### Melhorias futuras recomendadas

- Criar um painel inicial com atalhos: Criar avaliação, Gerar folhas, Scan, Resultados.
- Usar ícones consistentes em todos os botões.
- Criar tela de configurações gerais do professor.
- Melhorar responsividade do relatório em celulares.

---

## 2. Fluxo de uso do professor

### Fluxo ideal

1. Professor cadastra alunos/turma.
2. Cria avaliação.
3. Revisa gabarito oficial.
4. Gera folhas personalizadas.
5. Faz pré-teste de impressão.
6. Aplica a prova.
7. Usa Scan em tempo real.
8. Finaliza sessão.
9. Vê faltosos e resultados.
10. Gera devolutiva ou exporta notas.

### Pontos fortes

- O fluxo está completo.
- Há suporte a upload, PDF e câmera.
- O professor consegue usar o Atlas online pelo Render.

### Ponto corrigido

O Scan não usa mais o aluno da prova base quando o QR falha. Isso era o principal risco operacional, pois podia lançar nota no aluno errado.

---

## 3. Backend Flask

### Pontos fortes

- Rotas organizadas por função.
- Login de professor.
- Separação por `professor_id`.
- Uso de variáveis de ambiente.
- Compatibilidade com Render.

### Melhorias aplicadas

- Função `resolver_prova_por_qr_scan()` para validar o QR antes da correção.
- A prova base passou a servir somente como contexto da avaliação.
- O backend não salva resultado quando o QR não é confiável.
- O backend recusa folhas de outra avaliação/professor.

### Melhorias futuras recomendadas

- Separar o app em Blueprints: `auth`, `avaliacoes`, `scan`, `relatorios`, `ferramentas`.
- Mover regras de negócio para `services/`.
- Criar testes automatizados para as rotas principais.
- Reduzir o tamanho do `app.py`.

---

## 4. Banco de dados

### Pontos fortes

- Projeto agora suporta PostgreSQL persistente via `DATABASE_URL`.
- SQLite ficou apenas como fallback local.
- Existem tabelas para professores, alunos, avaliações, provas, folhas, resultados, sessões, ferramentas e funções avançadas.

### Pontos de atenção

- O adaptador SQLite/PostgreSQL mantém compatibilidade, mas o ideal em produção é migrar futuramente para SQLAlchemy ou Alembic.
- Não guardar dados importantes no SQLite local do Render.

### Recomendação

Usar PostgreSQL externo no Render, Neon ou Supabase. Depois de configurar `DATABASE_URL`, novos usuários, alunos e resultados deixam de sumir quando o serviço reinicia.

---

## 5. Visão computacional e OpenCV

### Pontos fortes

- O projeto usa OpenCV para leitura das bolhas.
- Há suporte a marcadores de canto e `warpPerspective`.
- A IA não é usada no Scan, mantendo velocidade.

### Melhorias aplicadas

- O Scan valida o QR antes de rodar a correção completa.
- O frontend diminui o frame antes de enviar, reduzindo latência.
- O loop automático é adaptativo e evita requisições paralelas.

### Melhorias futuras recomendadas

- Melhorar detecção dos 4 marcadores com fallback por maior quadrilátero da folha.
- Criar tela de calibração mostrando os pontos detectados.
- Salvar imagem de debug quando a leitura falhar.
- Criar pontuação de confiança por questão.

---

## 6. Segurança e integridade da avaliação

### Pontos fortes

- Provas Tipo A/B.
- Mapa de alternativas por folha.
- QR Code individual.
- Separação por professor.
- Anulação de questão em massa.

### Melhorias aplicadas

- QR novo com `professor_id`, `avaliacao_id`, `prova_id` e `aluno_id`.
- Recusa de QR inconsistente.
- Recusa de folha de outra avaliação.
- Fim do fallback automático para aluno.

### Melhorias futuras recomendadas

- Criar código curto impresso na folha além do QR.
- Permitir assinatura visual da folha com hash.
- Registrar logs de correção por professor.

---

## 7. Funcionalidades atuais

O Atlas atualmente cobre:

- login/cadastro de professor;
- cadastro e importação de alunos;
- criação de avaliação;
- geração de gabarito do professor;
- geração de folhas com QR Code;
- provas Tipo A/B;
- correção por upload;
- correção por câmera;
- pré-teste de impressão;
- anulação de questão em massa;
- lista de exercícios;
- importador de provas antigas;
- exportação para diário de classe;
- controle de faltosos;
- devolutiva expressa;
- identificação assistida;
- ferramentas do TCC;
- encerramento de ano letivo;
- banco persistente PostgreSQL.

---

## 8. Funcionalidades que precisam de teste real

Antes da apresentação, testar em celular e notebook:

- geração de folhas novas;
- leitura de QR no Scan;
- leitura de folha Tipo A e Tipo B;
- recusa de folha de outra avaliação;
- recusa quando QR falha;
- finalização de sessão;
- relatório de faltosos;
- anulação de questão;
- exportação CSV;
- PDF de devolutiva;
- PostgreSQL persistente no Render.

---

## 9. Prioridade técnica daqui para frente

1. Estabilizar o Scan com folhas reais impressas.
2. Configurar PostgreSQL definitivo.
3. Criar backup/exportação dos dados.
4. Melhorar relatórios pedagógicos.
5. Refatorar o backend em módulos menores.

---

## Conclusão

O Atlas está bem completo para um TCC. O maior risco técnico era o Scan atribuir uma folha ao aluno errado quando o QR falhava. Esse problema foi corrigido ao tornar o QR Code obrigatório para identificação automática e impedir fallback para aluno.

A segunda melhoria importante foi trocar o loop fixo do Scan por um loop adaptativo, mais rápido e mais seguro. Isso melhora a experiência do professor e reduz o tempo de correção em sala.

