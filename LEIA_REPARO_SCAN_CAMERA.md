# Reparo do Scan por Câmera - Atlas

Este pacote corrige dois problemas principais do Scan em tempo real:

1. **Aluno errado na correção por câmera**
2. **Leitura automática lenta, esperando cerca de 1 segundo ou mais entre tentativas**

## 1. Correção do aluno errado

Antes, quando o QR Code falhava, o Atlas usava a **prova base/fallback** selecionada na tela. Isso podia fazer a folha de um aluno ser lançada no nome de outro aluno.

Agora a regra é:

- o aluno só é identificado pelo QR Code validado no banco;
- a prova base serve apenas para indicar qual avaliação está sendo escaneada;
- se o QR falhar, o Atlas não salva resultado;
- se o QR pertencer a outra avaliação, a leitura é recusada;
- se o QR tiver aluno inconsistente, a leitura é recusada;
- se o QR for de outro professor, a leitura é recusada.

Status novos do Scan:

- `sem_qr`: QR não identificado;
- `qr_invalido`: QR lido, mas sem `prova_id` válido;
- `qr_nao_encontrado`: QR aponta para prova inexistente;
- `qr_inconsistente`: aluno do QR não bate com a prova salva;
- `professor_diferente`: folha pertence a outra conta;
- `prova_diferente`: folha pertence a outra avaliação.

## 2. Leitura automática mais rápida

Antes o frontend usava `setInterval` fixo. Agora usa **loop adaptativo com `setTimeout`**, que só dispara uma nova análise quando a anterior termina.

Velocidades disponíveis na tela:

- **Rápida:** tenta nova leitura em aproximadamente 350 ms;
- **Equilibrada:** aproximadamente 600 ms;
- **Econômica:** aproximadamente 1000 ms.

Também foi reduzido o tamanho do frame enviado ao backend para melhorar a latência sem prejudicar a leitura do QR.

## 3. Cooldown inteligente

Para evitar duplicidade e travamento:

- após leitura OK, aguarda cerca de 1,8 s para o professor retirar a folha;
- se a folha já foi lida, espera antes de tentar novamente;
- se não encontrou QR, tenta de novo rápido;
- não envia outra requisição enquanto uma análise está em andamento.

## 4. Mudança no QR Code

Folhas novas passam a gerar QR Code com mais campos:

```json
{
  "qr_version": 2,
  "sistema": "Atlas",
  "professor_id": 1,
  "avaliacao_id": 10,
  "prova_id": 55,
  "aluno_id": 3,
  "tipo_prova": "B"
}
```

Folhas antigas ainda podem funcionar se tiverem `prova_id` e `aluno_id`, mas o ideal é gerar folhas novas para ter a validação completa.

## 5. Arquivos alterados

- `app.py`
- `corretor.py`
- `templates/scan.html`
- `REVISAO_COMPLETA_ATLAS.md`

