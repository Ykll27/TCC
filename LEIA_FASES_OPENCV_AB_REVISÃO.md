# Atlas - Implementação das 4 fases

Este pacote implementa as quatro melhorias planejadas para o corretor Atlas.

## Fase 1 - OpenCV: 4 marcadores + warpPerspective

Arquivos alterados:

- `templates/folha.html`
- `corretor.py`

Melhorias:

- A folha de respostas agora possui 4 marcadores quadrados pretos nos cantos.
- `corretor.py` possui:
  - `ordenar_pontos(pts)`;
  - `localizar_marcadores_canto(imagem)`;
  - `alinhar_por_marcadores(imagem)`;
  - uso de `cv2.getPerspectiveTransform` e `cv2.warpPerspective`.
- A leitura de QR Code e bolhas tenta operar sobre a imagem alinhada.
- Se os marcadores não forem detectados, o sistema usa fallback na imagem original para não quebrar folhas antigas.

## Fase 2 - Provas Tipo A/B

Arquivos alterados:

- `bd.py`
- `app.py`
- `templates/folha.html`
- `templates/folhas_geradas.html`
- `templates/prova_tipo_aluno.html`

Melhorias:

- Tabela nova `folhas_resposta`.
- Novas colunas em `provas`:
  - `tipo_prova`;
  - `mapa_alternativas_json`.
- Cada folha recebe Tipo A/B.
- O QR Code inclui `tipo_prova`.
- O corretor converte a alternativa marcada na folha para a alternativa original antes de comparar com o gabarito oficial.
- Nova rota `/prova/<id>/tipo`, que gera uma versão personalizada da prova com alternativas na ordem daquela folha.

## Fase 3 - UX Scan: beep + canvas de confirmação

Arquivo alterado:

- `templates/scan.html`

Melhorias:

- Feedback sonoro de sucesso/erro.
- Vibração em dispositivos compatíveis.
- Borda visual verde/amarela/vermelha.
- Canvas/imagem de confirmação mostrando o último frame capturado.
- Timer de análise preservado.
- Câmera continua desligando automaticamente ao sair da página, ocultar a aba ou finalizar a sessão.

## Fase 4 - Revisão rápida com recorte

Arquivos alterados:

- `corretor.py`
- `app.py`
- `templates/scan.html`

Melhorias:

- O OMR retorna recortes base64 de questões duvidosas.
- O Scan abre um modal de revisão rápida quando a leitura precisa de revisão.
- Nova rota:
  - `POST /api/resultado/<resultado_id>/corrigir-questao`
- A revisão manual recalcula nota e atualiza o JSON do resultado.

## Como atualizar no Render

Depois de substituir os arquivos no repositório:

```bash
git add .
git commit -m "Implementa fases OpenCV, Tipo AB, Scan UX e Revisao"
git push
```

No Render:

```text
Manual Deploy → Deploy latest commit
```

## Observação sobre SQLAlchemy

A versão atual do Atlas neste pacote ainda usa o módulo `bd.py` com SQLite direto, pois era a estrutura real do projeto recebido. Mesmo assim, a tabela solicitada `folhas_resposta` e os campos `tipo_prova` e `mapa_alternativas_json` foram implementados no banco atual.

Se o projeto for migrado depois para SQLAlchemy, a entidade equivalente é:

```python
class FolhaResposta(db.Model):
    __tablename__ = "folhas_resposta"
    id = db.Column(db.Integer, primary_key=True)
    professor_id = db.Column(db.Integer, nullable=True)
    avaliacao_id = db.Column(db.Integer, nullable=True)
    prova_id = db.Column(db.Integer, nullable=False, unique=True)
    aluno_id = db.Column(db.Integer, nullable=False)
    tipo_prova = db.Column(db.String(8), default="A")
    mapa_alternativas_json = db.Column(db.Text, default="{}")
    criado_em = db.Column(db.String(32), nullable=False)
```
