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

    # Remove cercas ```json ... ``` caso o modelo retorne assim.
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


def _montar_alternativas_fallback(correta: str) -> Dict[str, str]:
    textos_errados = [
        "Apresenta uma conclusão sem ligação direta com as informações do texto.",
        "Ignora parte dos dados e generaliza a situação apresentada.",
        "Confunde o tema principal com uma informação secundária do contexto.",
        "Defende uma solução que não responde ao problema apresentado.",
    ]

    alternativas: Dict[str, str] = {}
    indice_errada = 0
    for alt in ALTERNATIVAS:
        if alt == correta:
            alternativas[alt] = "Relaciona corretamente causa, consequência e contexto do problema apresentado."
        else:
            alternativas[alt] = textos_errados[indice_errada % len(textos_errados)]
            indice_errada += 1
    return alternativas


def _criar_questao_fallback(numero: int, titulo: str, materias: str, temas: str) -> Dict[str, Any]:
    tema_base = (temas or "tema solicitado pelo professor").split(",")[0].strip() or "tema solicitado pelo professor"
    materia_base = (materias or "Conhecimentos gerais").split(",")[0].strip() or "Conhecimentos gerais"
    correta = ALTERNATIVAS[(numero - 1) % len(ALTERNATIVAS)]

    return {
        "numero": numero,
        "area": materia_base,
        "tema": tema_base,
        "contexto": (
            f"Uma escola está analisando uma situação relacionada a {tema_base}. "
            "Os estudantes precisam interpretar dados, relacionar informações e escolher a alternativa mais adequada."
        ),
        "enunciado": (
            f"Considerando o contexto apresentado e os conhecimentos de {materia_base}, "
            "assinale a alternativa que apresenta a interpretação mais adequada."
        ),
        "alternativas": _montar_alternativas_fallback(correta),
        "correta": correta,
        "habilidade": "Interpretar situação-problema em formato semelhante ao ENEM.",
        "explicacao": f"Alternativa {correta}: é a opção que melhor relaciona o contexto com o tema solicitado.",
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
    questoes = [
        _criar_questao_fallback(i, titulo, materias, temas)
        for i in range(1, total_questoes + 1)
    ]
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

        correta = str(original.get("correta", fallback["correta"])).strip().upper()
        if correta not in ALTERNATIVAS:
            correta = fallback["correta"]

        normalizadas.append(
            {
                "numero": i,
                "area": str(original.get("area", fallback["area"])).strip() or fallback["area"],
                "tema": str(original.get("tema", fallback["tema"])).strip() or fallback["tema"],
                "contexto": str(original.get("contexto", fallback["contexto"])).strip() or fallback["contexto"],
                "enunciado": str(original.get("enunciado", fallback["enunciado"])).strip() or fallback["enunciado"],
                "alternativas": alternativas_limpa,
                "correta": correta,
                "habilidade": str(original.get("habilidade", fallback["habilidade"])).strip() or fallback["habilidade"],
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
        "modo_geracao": dados.get("modo_geracao", "ia"),
        "aviso": dados.get("aviso", ""),
    }


def gerar_prova_enem(titulo: str, materias: str, temas: str, total_questoes: int, especificacoes: str) -> Dict[str, Any]:
    """
    Gera uma prova original no estilo ENEM.

    Fluxo:
    1. Tenta usar Gemini quando a chave estiver configurada.
    2. Se faltar chave, faltar pacote, acabar cota ou der erro 429, cria uma prova reserva local.
    3. Nunca deixa o Flask quebrar por causa da API de IA.
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
                aviso="A GOOGLE_API_KEY não foi encontrada. A prova foi gerada em modo demonstrativo.",
                modo="fallback_sem_chave",
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
                aviso="O pacote google-genai não foi carregado corretamente. A prova foi gerada em modo demonstrativo.",
                modo="fallback_sem_pacote_ia",
            ),
            titulo,
            materias,
            temas,
            total_questoes,
            especificacoes,
        )

    prompt = f"""
Você é um elaborador de avaliações escolares. Crie uma prova ORIGINAL no modelo ENEM, sem copiar questões reais de vestibulares, sites ou apostilas.

Especificações do professor:
- Título: {titulo}
- Matérias/áreas: {materias}
- Temas: {temas}
- Quantidade de questões: {total_questoes}
- Orientações extras: {especificacoes or "nenhuma"}

Regras obrigatórias:
1. Gere exatamente {total_questoes} questões.
2. Cada questão deve ter contexto curto, enunciado claro e 5 alternativas A, B, C, D, E.
3. Apenas uma alternativa pode estar correta.
4. Use linguagem escolar, contextualizada e parecida com ENEM, mas sem textos longos demais.
5. Não use imagens externas, links, tabelas complexas ou conteúdo copiado.
6. Retorne APENAS JSON válido, sem markdown.

Formato obrigatório:
{{
  "titulo": "...",
  "questoes": [
    {{
      "numero": 1,
      "area": "...",
      "tema": "...",
      "contexto": "...",
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
      "explicacao": "..."
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
                temperature=0.7,
            ),
        )

        dados = _extrair_json(getattr(resposta, "text", ""))
        return _normalizar_prova(dados, titulo, materias, temas, total_questoes, especificacoes)

    except Exception as erro:
        if _eh_erro_de_cota(erro):
            aviso = (
                f"A API do Gemini está sem cota ou em limite de uso para o modelo {MODELO_GEMINI}. "
                "A prova foi gerada em modo reserva para o sistema continuar funcionando."
            )
            modo = "fallback_cota_ia"
        elif _eh_erro_de_chave_ou_permissao(erro):
            aviso = (
                "A chave da API de IA parece inválida, sem permissão ou sem faturamento liberado. "
                "A prova foi gerada em modo reserva."
            )
            modo = "fallback_chave_ia"
        else:
            aviso = (
                "A IA falhou ao gerar a prova ou retornou uma resposta inválida. "
                "A prova foi gerada em modo reserva para evitar que o sistema quebre."
            )
            modo = "fallback_erro_ia"

        return _normalizar_prova(
            _prova_fallback(
                titulo,
                materias,
                temas,
                total_questoes,
                especificacoes,
                aviso=aviso,
                modo=modo,
            ),
            titulo,
            materias,
            temas,
            total_questoes,
            especificacoes,
        )
