import json
import os
import re
from typing import Any, Dict, List

from dotenv import load_dotenv

try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
MODELO_GEMINI = os.getenv("MODELO_GEMINI", "gemini-2.0-flash")
ALTERNATIVAS = ["A", "B", "C", "D", "E"]

_client = None
if GOOGLE_API_KEY and genai:
    try:
        _client = genai.Client(api_key=GOOGLE_API_KEY)
    except Exception:
        _client = None


def _extrair_json(texto: str) -> Dict[str, Any]:
    """Aceita resposta JSON pura ou JSON dentro de bloco markdown."""
    if not texto:
        raise ValueError("Resposta vazia da IA.")

    texto = texto.strip()
    texto = re.sub(r"^```(?:json)?", "", texto, flags=re.IGNORECASE).strip()
    texto = re.sub(r"```$", "", texto).strip()

    try:
        return json.loads(texto)
    except Exception:
        pass

    inicio = texto.find("{")
    fim = texto.rfind("}")
    if inicio >= 0 and fim > inicio:
        return json.loads(texto[inicio : fim + 1])

    raise ValueError("Não consegui encontrar um JSON válido na resposta da IA.")


def _texto_erro(erro: Exception) -> str:
    return str(erro or "").strip()


def _eh_erro_de_cota(erro: Exception) -> bool:
    txt = _texto_erro(erro).upper()
    gatilhos = [
        "429",
        "RESOURCE_EXHAUSTED",
        "QUOTA",
        "RATE LIMIT",
        "RATE_LIMIT",
        "TOO MANY REQUESTS",
        "GENERATE_CONTENT_FREE_TIER",
    ]
    return any(g in txt for g in gatilhos)


def _eh_erro_de_chave_ou_permissao(erro: Exception) -> bool:
    txt = _texto_erro(erro).upper()
    gatilhos = [
        "API_KEY_INVALID",
        "INVALID_ARGUMENT",
        "PERMISSION_DENIED",
        "UNAUTHENTICATED",
        "API KEY",
        "API_KEY",
        "BILLING",
    ]
    return any(g in txt for g in gatilhos)


def _str_curta(valor: Any, limite: int = 280) -> str:
    texto = str(valor or "").strip()
    texto = re.sub(r"\s+", " ", texto)
    return texto[:limite]


def _normalizar_linhas_tabela(linhas: Any, max_linhas: int = 6, max_cols: int = 5) -> List[List[str]]:
    if not isinstance(linhas, list):
        return []
    saida = []
    for linha in linhas[:max_linhas]:
        if isinstance(linha, dict):
            vals = list(linha.values())[:max_cols]
        elif isinstance(linha, list):
            vals = linha[:max_cols]
        else:
            vals = [linha]
        saida.append([_str_curta(v, 60) for v in vals])
    return saida


def _normalizar_visual(valor: Any, fallback: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Normaliza uma tabela/gráfico/diagrama para o Atlas renderizar sem depender de imagem externa."""
    if not isinstance(valor, dict):
        return fallback or {}

    tipo = _str_curta(valor.get("tipo") or valor.get("type") or "diagrama", 30).lower()
    if tipo not in {"tabela", "grafico_barras", "grafico_linha", "diagrama", "esquema", "imagem"}:
        tipo = "diagrama"

    visual: Dict[str, Any] = {
        "tipo": tipo,
        "titulo": _str_curta(valor.get("titulo") or valor.get("title") or "Elemento visual", 90),
        "descricao": _str_curta(valor.get("descricao") or valor.get("description") or valor.get("legenda") or "", 320),
        "fonte": _str_curta(valor.get("fonte") or "Elaboração própria/Atlas", 90),
    }

    if tipo == "tabela":
        colunas = valor.get("colunas") or valor.get("columns") or []
        if not isinstance(colunas, list):
            colunas = []
        visual["colunas"] = [_str_curta(c, 50) for c in colunas[:5]]
        visual["linhas"] = _normalizar_linhas_tabela(valor.get("linhas") or valor.get("rows"), 7, 5)
        if not visual["colunas"] and visual["linhas"]:
            visual["colunas"] = [f"Coluna {i+1}" for i in range(len(visual["linhas"][0]))]

    elif tipo in {"grafico_barras", "grafico_linha"}:
        dados = valor.get("dados") or valor.get("data") or []
        pontos = []
        if isinstance(dados, dict):
            dados = [{"rotulo": k, "valor": v} for k, v in dados.items()]
        if isinstance(dados, list):
            for item in dados[:7]:
                if isinstance(item, dict):
                    rotulo = item.get("rotulo") or item.get("label") or item.get("x") or "Item"
                    val = item.get("valor") or item.get("value") or item.get("y") or 0
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    rotulo, val = item[0], item[1]
                else:
                    continue
                try:
                    val_num = float(str(val).replace(",", "."))
                except Exception:
                    val_num = 0.0
                pontos.append({"rotulo": _str_curta(rotulo, 30), "valor": val_num})
        visual["dados"] = pontos
        visual["eixo_x"] = _str_curta(valor.get("eixo_x") or "Categorias", 60)
        visual["eixo_y"] = _str_curta(valor.get("eixo_y") or "Valor", 60)

    else:
        itens = valor.get("itens") or valor.get("items") or []
        if not isinstance(itens, list):
            itens = []
        visual["itens"] = [_str_curta(i, 80) for i in itens[:6]]
        if not visual["descricao"] and valor.get("imagem_descricao"):
            visual["descricao"] = _str_curta(valor.get("imagem_descricao"), 320)

    return visual


def _visual_fallback(numero: int, tema_base: str, total: int, parte: int) -> Dict[str, Any]:
    if numero % 3 == 1:
        return {
            "tipo": "tabela",
            "titulo": f"Dados do estudo sobre {tema_base}",
            "descricao": "Tabela criada pelo Atlas para servir de base ao cálculo da questão.",
            "colunas": ["Indicador", "Valor"],
            "linhas": [["Total analisado", str(total)], ["Casos destacados", str(parte)], ["Demais casos", str(total - parte)]],
            "fonte": "Elaboração própria/Atlas",
        }
    if numero % 3 == 2:
        return {
            "tipo": "grafico_barras",
            "titulo": f"Comparativo de resultados - {tema_base}",
            "descricao": "Gráfico simples com valores absolutos usados na interpretação.",
            "eixo_x": "Grupo",
            "eixo_y": "Quantidade",
            "dados": [
                {"rotulo": "A", "valor": parte},
                {"rotulo": "B", "valor": max(total - parte, 1)},
                {"rotulo": "Total", "valor": total},
            ],
            "fonte": "Elaboração própria/Atlas",
        }
    return {
        "tipo": "diagrama",
        "titulo": f"Esquema da situação-problema - {tema_base}",
        "descricao": "Representação esquemática gerada pelo Atlas para organizar as informações do enunciado.",
        "itens": ["Situação inicial", "Dados apresentados", "Cálculo necessário", "Conclusão"],
        "fonte": "Elaboração própria/Atlas",
    }


def _montar_alternativas_calculo(correta: str, valor_correto: float) -> Dict[str, str]:
    valores = [
        valor_correto,
        max(valor_correto - 10, 0),
        valor_correto + 10,
        max(100 - valor_correto, 0),
        round(valor_correto / 2, 1),
    ]
    # remove repetições mantendo cinco opções plausíveis
    unicos = []
    for v in valores:
        v = round(float(v), 1)
        if v not in unicos:
            unicos.append(v)
    while len(unicos) < 5:
        unicos.append(round((len(unicos) + 1) * 12.5, 1))

    alternativas: Dict[str, str] = {}
    idx = 0
    for alt in ALTERNATIVAS:
        if alt == correta:
            alternativas[alt] = f"{valor_correto:.1f}%"
        else:
            while idx < len(unicos) and abs(unicos[idx] - valor_correto) < 0.01:
                idx += 1
            alternativas[alt] = f"{unicos[idx % len(unicos)]:.1f}%"
            idx += 1
    return alternativas


def _criar_questao_fallback(numero: int, titulo: str, materias: str, temas: str) -> Dict[str, Any]:
    tema_base = (temas or "tema solicitado pelo professor").split(",")[0].strip() or "tema solicitado pelo professor"
    materia_base = (materias or "Conhecimentos gerais").split(",")[0].strip() or "Conhecimentos gerais"
    correta = ALTERNATIVAS[(numero - 1) % len(ALTERNATIVAS)]
    total = 80 + (numero * 12)
    parte = 18 + (numero * 5)
    percentual = round((parte / total) * 100, 1)

    return {
        "numero": numero,
        "area": materia_base,
        "tema": tema_base,
        "dificuldade": "medio",
        "tipo_questao": "calculo_com_interpretacao",
        "contexto": (
            f"Uma turma realizou um levantamento relacionado a {tema_base}. "
            f"Foram analisados {total} registros, dos quais {parte} apresentaram a característica destacada no estudo."
        ),
        "elemento_visual": _visual_fallback(numero, tema_base, total, parte),
        "dados_calculo": {
            "formula": "percentual = (parte / total) × 100",
            "valores": {"parte": parte, "total": total},
            "unidade": "%",
        },
        "enunciado": (
            f"Com base nos dados apresentados, qual é aproximadamente o percentual de registros ligados a {tema_base}?"
        ),
        "alternativas": _montar_alternativas_calculo(correta, percentual),
        "correta": correta,
        "habilidade": "Interpretar dados, selecionar informações relevantes e realizar cálculo percentual em situação-problema.",
        "resolucao": [
            f"Identificar o total de registros: {total}.",
            f"Identificar a parte destacada: {parte}.",
            f"Aplicar a fórmula: ({parte} / {total}) × 100.",
            f"Resultado aproximado: {percentual:.1f}%.",
        ],
        "explicacao": f"Alternativa {correta}: o percentual correto é obtido por ({parte} / {total}) × 100 = {percentual:.1f}%.",
        "competencia": "Leitura de dados e resolução de problema.",
    }


def _prova_fallback(
    titulo: str,
    materias: str,
    temas: str,
    total_questoes: int,
    especificacoes: str,
    aviso: str,
    modo: str = "fallback_sem_ia",
) -> Dict[str, Any]:
    questoes = [_criar_questao_fallback(i, titulo, materias, temas) for i in range(1, total_questoes + 1)]
    gabarito = {str(q["numero"]): q["correta"] for q in questoes}
    return {
        "titulo": titulo,
        "materias": materias,
        "temas": temas,
        "total_questoes": total_questoes,
        "orientacoes": especificacoes,
        "questoes": questoes,
        "gabarito": gabarito,
        "modo_geracao": modo,
        "aviso": aviso,
    }


def _normalizar_calculo(original: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    bruto = original.get("dados_calculo") or original.get("calculo") or original.get("dados")
    if not isinstance(bruto, dict):
        return fallback.get("dados_calculo", {})
    formula = _str_curta(bruto.get("formula") or bruto.get("expressao") or "", 160)
    valores = bruto.get("valores") if isinstance(bruto.get("valores"), dict) else {}
    return {
        "formula": formula,
        "valores": {str(k): _str_curta(v, 60) for k, v in list(valores.items())[:8]},
        "unidade": _str_curta(bruto.get("unidade") or "", 20),
    }


def _normalizar_resolucao(valor: Any, fallback: Dict[str, Any]) -> List[str]:
    if isinstance(valor, list):
        passos = [_str_curta(v, 220) for v in valor if str(v or "").strip()]
    else:
        texto = str(valor or "").strip()
        passos = [p.strip() for p in re.split(r"(?:\n|;|\d+\))", texto) if p.strip()]
        passos = [_str_curta(p, 220) for p in passos]
    return passos[:6] or fallback.get("resolucao", [])


def _normalizar_prova(dados: Dict[str, Any], titulo: str, materias: str, temas: str, total_questoes: int, especificacoes: str) -> Dict[str, Any]:
    questoes = dados.get("questoes", [])
    if not isinstance(questoes, list):
        questoes = []

    normalizadas: List[Dict[str, Any]] = []

    for i in range(1, total_questoes + 1):
        original = questoes[i - 1] if i - 1 < len(questoes) and isinstance(questoes[i - 1], dict) else {}
        fallback = _criar_questao_fallback(i, titulo, materias, temas)

        alternativas = original.get("alternativas", fallback["alternativas"])
        if not isinstance(alternativas, dict):
            alternativas = fallback["alternativas"]

        alternativas_limpa = {}
        for alt in ALTERNATIVAS:
            texto_alt = str(alternativas.get(alt, fallback["alternativas"][alt])).strip()
            alternativas_limpa[alt] = texto_alt or fallback["alternativas"][alt]

        correta = str(original.get("correta", fallback["correta"])).strip().upper()[:1]
        if correta not in ALTERNATIVAS:
            correta = fallback["correta"]

        visual_bruto = (
            original.get("elemento_visual")
            or original.get("visual")
            or original.get("imagem")
            or original.get("figura")
            or original.get("grafico")
            or original.get("tabela")
        )
        elemento_visual = _normalizar_visual(visual_bruto, fallback.get("elemento_visual", {}))

        normalizadas.append(
            {
                "numero": i,
                "area": str(original.get("area", fallback["area"])).strip() or fallback["area"],
                "tema": str(original.get("tema", fallback["tema"])).strip() or fallback["tema"],
                "dificuldade": str(original.get("dificuldade", fallback.get("dificuldade", "medio"))).strip() or "medio",
                "tipo_questao": str(original.get("tipo_questao", fallback.get("tipo_questao", "interpretacao"))).strip() or "interpretacao",
                "contexto": str(original.get("contexto", fallback["contexto"])).strip() or fallback["contexto"],
                "elemento_visual": elemento_visual,
                "dados_calculo": _normalizar_calculo(original, fallback),
                "enunciado": str(original.get("enunciado", fallback["enunciado"])).strip() or fallback["enunciado"],
                "alternativas": alternativas_limpa,
                "correta": correta,
                "habilidade": str(original.get("habilidade", fallback["habilidade"])).strip() or fallback["habilidade"],
                "competencia": str(original.get("competencia", fallback.get("competencia", ""))).strip(),
                "resolucao": _normalizar_resolucao(original.get("resolucao") or original.get("passos_resolucao"), fallback),
                "explicacao": str(original.get("explicacao", fallback["explicacao"])).strip() or fallback["explicacao"],
            }
        )

    gabarito = {str(q["numero"]): q["correta"] for q in normalizadas}

    return {
        "titulo": str(dados.get("titulo", titulo)).strip() or titulo,
        "materias": materias,
        "temas": temas,
        "total_questoes": total_questoes,
        "orientacoes": especificacoes,
        "questoes": normalizadas,
        "gabarito": gabarito,
        "modo_geracao": dados.get("modo_geracao", "ia_questoes_completas"),
        "aviso": dados.get("aviso", ""),
    }


def gerar_prova_enem(titulo: str, materias: str, temas: str, total_questoes: int, especificacoes: str) -> Dict[str, Any]:
    """
    Gera uma prova original no estilo ENEM, agora com questões mais completas.

    Recursos aceitos na estrutura da questão:
    - cálculo/dados numéricos;
    - elemento visual renderizável pelo Atlas: tabela, gráfico, diagrama ou esquema;
    - resolução passo a passo para o gabarito do professor;
    - habilidade/competência e explicação.

    O Atlas não baixa imagens externas. Quando a questão pede imagem, a IA deve retornar
    uma descrição/estrutura de figura, e o sistema renderiza isso como elemento visual próprio.
    """
    total_questoes = max(1, min(int(total_questoes or 1), 90))
    titulo = (titulo or "Simulado Modelo ENEM").strip()
    materias = (materias or "Conhecimentos gerais").strip()
    temas = (temas or "Temas variados").strip()
    especificacoes = (especificacoes or "").strip()

    if not GOOGLE_API_KEY:
        return _normalizar_prova(
            _prova_fallback(
                titulo,
                materias,
                temas,
                total_questoes,
                especificacoes,
                aviso="A GOOGLE_API_KEY não foi encontrada. A prova foi gerada em modo demonstrativo com questões completas locais.",
                modo="fallback_completo_sem_chave",
            ),
            titulo,
            materias,
            temas,
            total_questoes,
            especificacoes,
        )

    if not _client or not types:
        return _normalizar_prova(
            _prova_fallback(
                titulo,
                materias,
                temas,
                total_questoes,
                especificacoes,
                aviso="O pacote google-genai não foi carregado corretamente. A prova foi gerada em modo demonstrativo com questões completas locais.",
                modo="fallback_completo_sem_pacote_ia",
            ),
            titulo,
            materias,
            temas,
            total_questoes,
            especificacoes,
        )

    prompt = f"""
Você é um elaborador sênior de avaliações escolares. Crie uma prova ORIGINAL no modelo ENEM, sem copiar questões reais de vestibulares, sites, apostilas ou livros.

Especificações do professor:
- Título: {titulo}
- Matérias/áreas: {materias}
- Temas: {temas}
- Quantidade de questões: {total_questoes}
- Orientações extras: {especificacoes or "nenhuma"}

Regras obrigatórias de qualidade:
1. Gere exatamente {total_questoes} questões.
2. Cada questão deve ter contexto, situação-problema, enunciado claro e 5 alternativas A, B, C, D, E.
3. Apenas uma alternativa pode estar correta.
4. Faça questões mais completas, com leitura de dados, interpretação e raciocínio; evite perguntas rasas de memorização.
5. Pelo menos metade das questões deve ter cálculo, fórmula, comparação numérica, tabela, gráfico, esquema ou diagrama.
6. Quando a questão usar imagem, NÃO use link nem imagem externa. Retorne um elemento visual estruturado para o Atlas renderizar.
7. Use números realistas e cálculos conferíveis. Não coloque cálculo impossível ou dados contraditórios.
8. A resolução passo a passo deve ficar no campo "resolucao" e não deve aparecer no enunciado do aluno.
9. Retorne APENAS JSON válido, sem markdown e sem comentários.

Tipos de elemento_visual aceitos pelo Atlas:
- tabela: use colunas e linhas.
- grafico_barras: use dados com rotulo e valor.
- grafico_linha: use dados com rotulo e valor.
- diagrama/esquema/imagem: use descricao e itens. O Atlas desenhará uma figura esquemática.

Formato obrigatório:
{{
  "titulo": "...",
  "questoes": [
    {{
      "numero": 1,
      "area": "...",
      "tema": "...",
      "dificuldade": "facil|medio|dificil",
      "tipo_questao": "calculo|grafico|tabela|imagem|interpretacao|mista",
      "contexto": "texto-base contextualizado",
      "elemento_visual": {{
        "tipo": "tabela|grafico_barras|grafico_linha|diagrama|esquema|imagem",
        "titulo": "...",
        "descricao": "...",
        "colunas": ["..."],
        "linhas": [["...", "..."]],
        "dados": [{{"rotulo":"...", "valor": 10}}],
        "itens": ["..."],
        "fonte": "Elaboração própria"
      }},
      "dados_calculo": {{
        "formula": "...",
        "valores": {{"...":"..."}},
        "unidade": "..."
      }},
      "enunciado": "...",
      "alternativas": {{
        "A": "...",
        "B": "...",
        "C": "...",
        "D": "...",
        "E": "..."
      }},
      "correta": "A",
      "habilidade": "...",
      "competencia": "...",
      "resolucao": ["passo 1", "passo 2", "passo 3"],
      "explicacao": "explicação objetiva da alternativa correta"
    }}
  ],
  "gabarito": {{"1":"A"}}
}}
""".strip()

    try:
        resposta = _client.models.generate_content(
            model=MODELO_GEMINI,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.55,
            ),
        )

        dados = _extrair_json(getattr(resposta, "text", ""))
        return _normalizar_prova(dados, titulo, materias, temas, total_questoes, especificacoes)

    except Exception as erro:
        if _eh_erro_de_cota(erro):
            aviso = (
                f"A API do Gemini está sem cota ou em limite de uso para o modelo {MODELO_GEMINI}. "
                "A prova foi gerada em modo reserva local com questões completas para o sistema continuar funcionando."
            )
            modo = "fallback_completo_cota_ia"
        elif _eh_erro_de_chave_ou_permissao(erro):
            aviso = (
                "A chave da API de IA parece inválida, sem permissão ou sem faturamento liberado. "
                "A prova foi gerada em modo reserva local com questões completas."
            )
            modo = "fallback_completo_chave_ia"
        else:
            aviso = (
                "A IA falhou ao gerar a prova ou retornou uma resposta inválida. "
                "A prova foi gerada em modo reserva local com questões completas para evitar que o sistema quebre."
            )
            modo = "fallback_completo_erro_ia"

        return _normalizar_prova(
            _prova_fallback(titulo, materias, temas, total_questoes, especificacoes, aviso=aviso, modo=modo),
            titulo,
            materias,
            temas,
            total_questoes,
            especificacoes,
        )
