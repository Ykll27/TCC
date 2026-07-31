# Atlas — Questões mais completas

Esta versão melhora o gerador de provas do Atlas para criar avaliações mais ricas e úteis.

## Novos campos por questão

Cada questão pode conter:

- `dificuldade`: facil, medio ou dificil;
- `tipo_questao`: calculo, grafico, tabela, imagem, interpretacao ou mista;
- `elemento_visual`: tabela, gráfico, diagrama, esquema ou imagem esquemática renderizada pelo Atlas;
- `dados_calculo`: fórmula, valores e unidade usados na questão;
- `resolucao`: passos da resolução para o gabarito do professor;
- `competencia`: competência relacionada;
- `habilidade`: habilidade cobrada;
- `explicacao`: justificativa da resposta correta.

## Como o Atlas lida com imagens

O Atlas não baixa imagens externas automaticamente. Isso evita direitos autorais, links quebrados e problemas de impressão.

Quando a IA retorna uma "imagem", ela vem como estrutura JSON. O próprio Atlas renderiza essa imagem como tabela, gráfico ou esquema visual dentro da prova e do PDF.

## Onde aparece

- Na tela da avaliação;
- Na prova personalizada do aluno;
- No PDF da prova;
- No gabarito do professor, com resolução passo a passo;
- Na tela de revisão da avaliação.

## Melhoramento de prova

A tela **Melhorar prova** agora identifica questões pobres e pode preencher automaticamente:

- contexto curto;
- elemento visual ausente;
- dados de cálculo ausentes;
- resolução passo a passo vazia;
- explicação vazia;
- alternativas/gabarito inválidos.
