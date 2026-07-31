# Upload de imagens nas questões

Esta versão adiciona suporte para o professor enviar imagens diretamente para o Atlas na tela de revisão da avaliação.

## Como usar

1. Gere ou abra uma avaliação.
2. Clique em **Revisar**.
3. Em cada questão, use a área **Imagem enviada pelo professor**.
4. Selecione uma imagem em PNG, JPG, JPEG ou WEBP.
5. Informe título, fonte/crédito e descrição curta.
6. Salve a revisão.

A imagem será exibida:

- na tela da prova;
- na prova personalizada do aluno;
- no PDF da prova;
- junto do elemento visual da questão.

## Segurança

- O Atlas aceita apenas PNG, JPG, JPEG e WEBP.
- O arquivo é validado com Pillow antes de ser salvo como imagem da questão.
- O caminho salvo no banco fica dentro de `static/questoes/`.
- Se o professor remover a imagem, o arquivo antigo é apagado do sistema quando possível.

## Observação

Evite enviar imagens com direitos autorais sem autorização. Para o TCC e uso escolar, prefira imagens próprias, gráficos criados pelo professor ou materiais com permissão de uso.
