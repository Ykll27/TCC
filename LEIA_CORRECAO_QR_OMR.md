# Atlas - Correção QR/OMR

Esta versão corrige o problema em que o Atlas não lia o QR Code corretamente e marcava a questão como NULA mesmo quando a alternativa A estava preenchida.

## O que foi corrigido

- Leitura de QR Code com pipeline multietapa:
  - leitura direta;
  - recorte do cabeçalho e do canto superior direito;
  - aumento de escala;
  - CLAHE/equalização;
  - threshold adaptativo;
  - rotações leves.
- QR Code novo com payload compacto e maior correção de erro.
- Folha nova com QR maior.
- Folha nova com letras A/B/C/D/E fora das bolhas.
- Fallback OMR com HoughCircles para detectar os círculos reais.
- Correção do caso em que letras dentro das bolhas faziam o sistema retornar NULA.
- PDF agora é convertido em resolução 3x.
- Scan mais rápido: intervalo automático reduzido e frame em qualidade maior.
- QR falhou: o sistema NÃO salva no aluno da prova base.
- QR falhou: a folha vai para Identificação Manual.

## Teste feito

Com o PDF enviado pelo usuário:

- QR lido com sucesso.
- Aluno: Argeli Pedro de Lima.
- Matrícula: 234567.
- Prova: 1.
- Questão 1 detectada como A.
- Com gabarito A, nota final: 100%.

## Arquivos principais alterados

- `corretor.py`
- `app.py`
- `bd.py`
- `templates/folha.html`
- `templates/scan.html`
- `templates/base.html`
- `templates/index.html`
- `templates/identificacoes_pendentes.html`
- `templates/resolver_identificacao.html`

## Importante

Depois de subir essa versão, gere folhas novas. As folhas antigas ainda melhoraram com o fallback HoughCircles, mas as folhas novas ficam muito mais fáceis para o OpenCV ler.
