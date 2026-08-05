# Reparo do Scan usando o mesmo pipeline do Upload

Este pacote ajusta o fluxo de Scan por câmera para usar a mesma lógica de correção do Upload.

## Alterações principais

### Frontend - templates/scan.html
- Câmera pede alta resolução: 1920x1080.
- Canvas preserva `video.videoWidth` e `video.videoHeight`.
- Frame é enviado em JPEG com qualidade 0.98.
- Removido loop infinito com `setInterval`.
- O botão `Analisar agora` processa um único frame.
- O automático roda em rajadas curtas e pausa se o QR não for encontrado.
- Botão `Enviar frame para identificação manual` só cria pendência quando o professor pedir.

### Backend - app.py
- Criado `corrigir_folha_por_pipeline_unico`.
- Upload e Scan chamam o mesmo pipeline de QR + OpenCV.
- Scan não cria pendência para todo frame sem QR.
- Se o QR falhar, retorna `buscando_qr` e não salva aluno/prova base.
- Se o professor pedir identificação manual, o frame é salvo como pendência.
- Resposta JSON inclui `auto_continuar: false` para impedir loop infinito.

### OpenCV - corretor.py
- Adicionados recortes específicos para o modelo V1.
- QR do bloco direito da folha recebe crop maior.
- Recortes são ampliados até 4x.
- Adicionada variante com nitidez para QR levemente embaçado.

## Fluxo esperado

1. Professor inicia a câmera.
2. O Atlas captura frame em alta resolução.
3. O frame vai para o mesmo pipeline usado no Upload.
4. O backend tenta ler QR no frame inteiro e em crops ampliados.
5. Se o QR ler, corrige e salva.
6. Se o QR falhar, não salva nada automaticamente.
7. O professor pode ajustar a folha ou enviar o frame para identificação manual.
