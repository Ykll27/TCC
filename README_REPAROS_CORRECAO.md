# Atlas — versão antiga revisada + correção reparada

Esta versão mantém a base visual antiga do Atlas e adiciona melhorias focadas nas funções mais importantes do corretor.

## Correção reparada

- O Atlas não usa mais a prova base para salvar resultado quando o QR Code falha.
- Se o QR não for lido, a folha vai para **Identificação Assistida**.
- Se o QR for inválido, apontar para outro professor, outro aluno ou outra prova, a nota não é salva automaticamente.
- O QR novo recebe assinatura de segurança.
- QRs antigos continuam aceitos quando `prova_id` e `aluno_id` batem com o banco.

## Scan mais rápido

- O frame enviado pela câmera foi reduzido para acelerar o processamento.
- O JPEG vai com compressão mais leve.
- O loop deixou de ser fixo em 1,4 segundo e virou adaptativo.
- O frontend não envia outro frame enquanto o anterior ainda está processando.
- Duplicatas não entram no total da sessão.

## Quando o QR Code falhar

- A imagem é salva em `identificacoes_pendentes`.
- O professor acessa `QR pendente` no menu.
- O sistema mostra a foto e sugestões de alunos da mesma turma.
- O professor escolhe a folha/prova correta.
- Só então o Atlas corrige e salva o resultado.

## Embaralhamento de questões

- Na geração das folhas existe a opção **Embaralhar questões por aluno**.
- O Atlas salva a ordem de questões em `ordem_questoes_json` e `mapa_questoes_json`.
- Cada aluno pode receber uma prova personalizada.
- A correção usa o gabarito da folha gerada, então a nota continua automática.

## Revisão manual

- Cada resultado pode ser revisado manualmente.
- O professor altera A/B/C/D/E/NULA por questão.
- Ao salvar, a nota é recalculada automaticamente.
- O resultado fica marcado como `revisado_manual`.

## Melhoramento de prova

- Tela `Melhorar prova` analisa problemas básicos:
  - enunciado curto;
  - alternativa vazia;
  - alternativas repetidas;
  - gabarito inválido;
  - questão sem explicação.
- O botão de melhoria normaliza textos, preenche explicações básicas e valida gabarito.

## Exportação para diário de classe

- No relatório da avaliação há o botão `Exportar diário CSV`.
- O CSV sai no formato:
  - Matrícula;
  - Nome;
  - Turma;
  - Nota Final;
  - Status.

## Arquivos mais alterados

- `app.py`
- `bd.py`
- `corretor.py`
- `templates/scan.html`
- `templates/selecionar_alunos.html`
- `templates/folhas_geradas.html`
- `templates/prova_gerada.html`
- `templates/relatorio_avaliacao.html`
- `templates/resultado_detalhe.html`

## Templates novos

- `templates/prova_personalizada.html`
- `templates/identificacoes_pendentes.html`
- `templates/resolver_identificacao.html`
- `templates/revisar_resultado.html`
- `templates/melhorar_avaliacao.html`
