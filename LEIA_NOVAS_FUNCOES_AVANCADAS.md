# Atlas - Novas Funções Avançadas

Este pacote adiciona funções voltadas para praticidade real do professor e manutenção do ano letivo.

## 1. Anulação de questão em massa
Local: relatório da avaliação.

Permite anular/restaurar uma questão. O Atlas recalcula automaticamente todos os resultados ligados à avaliação, removendo a questão anulada do total de questões válidas.

Novas estruturas:
- `questoes_anuladas`
- recalculo automático dos JSONs de `resultados`

## 2. Pré-teste de impressão
Local: avaliação e tela de folhas geradas.

Permite testar uma folha recém-impressa antes de aplicar para toda a turma. O Atlas valida:
- leitura do QR Code;
- presença dos 4 marcadores de canto.

## 3. Lista de exercícios a partir do banco local
Local: menu dos 3 risquinhos → Lista de exercícios.

Permite filtrar questões por matéria, tema e dificuldade e gerar um PDF simples com ou sem gabarito.

## 4. Identificação assistida quando o QR falha
Local: menu dos 3 risquinhos → Identificação assistida.

Quando o QR Code não funciona, o professor pode enviar a folha. O sistema tenta OCR opcional com `pytesseract` se disponível e sugere alunos prováveis por similaridade.

Observação: OCR é opcional; o Atlas continua funcionando sem `pytesseract`.

## 5. Arquivo mestre de encerramento de ano letivo
Local: menu dos 3 risquinhos → Encerramento de ano.

Arquiva avaliações, folhas e resultados antigos para limpar o painel principal.

## 6. Gerador de prova de 2ª chamada
Local: tela da avaliação.

Gera uma nova avaliação de 2ª chamada com o mesmo tema e nível geral, priorizando banco local/cache e sem consumir IA.

## 7. Importador de provas antigas
Local: menu dos 3 risquinhos → Importar prova antiga.

O professor cola uma prova antiga do Word/PDF. O Atlas tenta separar questões numeradas e alternativas A-E para salvar no banco de questões.

## 8. Exportação direta para diário de classe
Local: relatório da avaliação.

Gera CSV com:
- Matrícula;
- Nome;
- Nota Final.

## 9. Controle automático de faltosos
Local: resumo do Scan e relatório da avaliação.

O Atlas cruza as folhas geradas com as correções realizadas e lista alunos que ainda não possuem resultado.

## 10. Devolutiva expressa
Local: relatório da avaliação.

Gera PDF com tiras individuais contendo:
- nome do aluno;
- turma;
- nota;
- acertos, erros e brancos/nulas;
- questões para revisar.

## Como atualizar no Render

```bash
git add .
git commit -m "Adiciona funcoes avancadas do Atlas"
git push
```

Depois no Render:

Manual Deploy → Deploy latest commit
