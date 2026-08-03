# Reparo: Scan analisando frame infinito

## Problema
O Scan analisava frames sem parar quando o QR Code não era encontrado. Além disso, o backend criava uma pendência de identificação manual a cada frame sem QR, acumulando registros repetidos.

## Correção aplicada

### Frontend (`templates/scan.html`)
- Removido loop infinito com `setInterval`.
- Adicionado loop controlado com `setTimeout` adaptativo.
- Bloqueio contra requisições simultâneas com `emProcessamento`.
- Contador de falhas consecutivas de QR.
- Depois de 8 tentativas sem QR, o Scan pausa automaticamente.
- Adicionado botão `Enviar frame para identificação manual`.
- Depois de sucesso, há cooldown para o professor retirar a folha antes da próxima leitura.
- Duplicatas não entram em loop rápido.

### Backend (`app.py`)
- Quando o QR falha no loop automático, o Atlas NÃO cria pendência no banco.
- O frame sem QR é apagado para não lotar a pasta `uploads`.
- A pendência só é criada quando o professor clica em `Enviar frame para identificação manual`.
- QR falhou continua sem salvar nota automaticamente.

## Regra mantida
QR falhou = não salva no aluno errado.

## Como testar
1. Abra o Scan.
2. Inicie a câmera.
3. Aponte para uma mesa ou folha sem QR.
4. O sistema deve tentar algumas vezes e pausar.
5. Aponte para uma folha com QR e clique em Retomar automático.
6. A correção deve continuar normalmente.
