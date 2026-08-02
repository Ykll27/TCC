# Atlas — Rollback seletivo do corretor

Esta versão volta o sistema para a base antiga do Atlas, preservando o fluxo visual antigo, mas mantém apenas os reparos realmente necessários para não salvar nota no aluno errado.

## O que foi revertido

- A lógica principal do corretor voltou a seguir o fluxo antigo do Atlas.
- O sistema deixa de depender de mudanças grandes de layout/design para corrigir.
- A leitura de bolhas mantém compatibilidade com as folhas antigas.

## O que foi mantido como correção obrigatória

- QR Code falhou: o Atlas não usa mais a prova base para salvar nota automaticamente.
- QR Code falhou: a folha vai para Identificação Manual.
- Depois da Identificação Manual, o sistema usa o comportamento antigo tolerante (`usar_ia="auto"`) caso o OpenCV fique inseguro.
- QR Code lido: corrige automaticamente no aluno e prova corretos.
- OMR possui fallback por HoughCircles para folhas antigas com letras dentro das bolhas.
- PDFs são convertidos com resolução maior para melhorar QR e bolhas.
- Nenhuma correção deve ser salva com aluno/prova inventados.

## Resultado esperado no arquivo de teste do Argeli

Para a folha enviada `argelnho.pdf`:

- QR lido: sim, quando renderizado com qualidade suficiente.
- Aluno: Argeli Pedro de Lima.
- Matrícula: 234567.
- Prova: 1.
- Questão 1: A.
- Se o gabarito da questão 1 for A, a nota esperada é 100%.

## Observação importante

Se o professor resolver uma folha manualmente porque o QR falhou, ele deve selecionar a folha/prova/aluno correta na tela de Identificação Manual. O Atlas só salva depois dessa confirmação.
