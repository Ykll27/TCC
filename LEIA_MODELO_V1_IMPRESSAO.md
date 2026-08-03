# Atlas - Modelo V1 de Impressão integrado

Esta versão usa como referência visual os arquivos enviados em `docs/modelos_referencia`:

- `01-teste-v1-prova.pdf`
- `02-teste-v1-gabarito.pdf`
- `03-teste-v1-folhas-resposta.pdf`

## O que foi integrado

1. A folha de resposta (`templates/folha.html`) foi refeita no padrão V1:
   - topo com marca Atlas;
   - título da avaliação;
   - caixa de versão `V1` no canto superior direito;
   - linha azul de separação;
   - cards de disciplina, assunto, turma e data;
   - card do aluno e matrícula;
   - lista de bolhas em duas colunas;
   - identificador textual da folha no rodapé.

2. O QR Code foi mantido de forma técnica e obrigatória no card do aluno.
   - Os PDFs de referência não exibem QR Code, apenas identificador textual.
   - No Atlas real, o QR precisa continuar existindo para evitar salvar nota no aluno errado.
   - O QR agora usa payload compacto e também contém o identificador da folha.

3. O banco recebeu o campo `folha_codigo` na tabela `provas`.
   - Exemplo: `FLH-260801-07D60FDB`.
   - Esse código aparece no rodapé e serve como plano B para conferência manual.

4. O PDF da prova e o PDF do gabarito foram reformulados para seguir o visual V1:
   - `gerar_pdf_prova(..., incluir_gabarito=False)` gera a prova;
   - `gerar_pdf_prova(..., incluir_gabarito=True)` gera o gabarito do professor.

5. O OMR foi ajustado para o novo modelo de folha:
   - suporte a duas colunas balanceadas;
   - compatibilidade com letras dentro das bolhas;
   - suporte ao modelo antigo com colunas de 10 questões.

## Regra importante

A correção automática continua assim:

- QR lido: corrige automaticamente.
- QR não lido: não salva no aluno base e envia para identificação manual.
- Identificador textual no rodapé: serve para conferência manual, não substitui o QR na correção automática.

## Observação sobre IA

A correção principal usa OpenCV + regras. A IA pode ser usada apenas como apoio visual quando o OMR fica sem confiança, mas não deve ser usada para chutar respostas.
