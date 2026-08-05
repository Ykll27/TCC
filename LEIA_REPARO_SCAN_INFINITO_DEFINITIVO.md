# Reparo definitivo do Scan infinito

Este pacote corrige o bug em que o Scan ficava analisando frames sem parar.

## Causa

A versão anterior ainda continuava agendando novos frames em alguns retornos do backend, principalmente quando:

- o QR não era encontrado repetidamente;
- a mesma folha era mantida na câmera depois de corrigir;
- o backend retornava `duplicado`;
- o resultado precisava de revisão;
- o navegador continuava enviando frames mesmo após erro.

## Correção aplicada

### Frontend `templates/scan.html`

- O Scan não inicia mais em loop infinito automaticamente após ligar a câmera.
- A câmera liga e espera o professor clicar em `Analisar agora` ou `Retomar automático`.
- O modo automático agora funciona em rajadas curtas.
- Cada rajada analisa no máximo 5 frames.
- Se não encontrar QR em 4 falhas seguidas, pausa sozinho.
- Se corrigir com sucesso, pausa e pede para trocar a folha.
- Se detectar duplicado, pausa e pede para trocar a folha.
- Se cair em revisão, pausa.
- Nunca envia frame novo enquanto o anterior ainda está processando.

### Backend `app.py`

- Adicionado estado de proteção por sessão: `SCAN_RUNTIME_STATE`.
- Adicionado limite de falhas de QR no servidor.
- Se o frontend antigo continuar chamando a rota, o backend retorna `scan_pausado`.
- Adicionado intervalo mínimo entre frames.
- QR falhou no loop automático: não cria pendência, não salva resultado e remove frame temporário.
- QR falhou e o professor clicou em identificação manual: salva apenas uma pendência solicitada manualmente.

## Comportamento esperado

1. Clique em `Iniciar câmera`.
2. A câmera liga, mas não fica analisando infinitamente.
3. Clique em `Retomar automático`.
4. O Atlas tenta poucos frames rapidamente.
5. Se não achar QR, pausa sozinho.
6. Se corrigir uma folha, pausa para trocar a folha.
7. Para continuar, clique em `Retomar automático` novamente.

