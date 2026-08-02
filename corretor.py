import os
import json
from typing import Dict, Any, Optional, List, Tuple

import cv2
import numpy as np
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
QUESTOES_POR_COLUNA = 10


gemini_client = None
if GOOGLE_API_KEY and genai:
    gemini_client = genai.Client(api_key=GOOGLE_API_KEY)


def ler_imagem(caminho_imagem: str):
    """Lê imagem aceitando caminhos com acentos no Windows."""
    try:
        dados = np.fromfile(caminho_imagem, dtype=np.uint8)
        if dados.size == 0:
            return None
        return cv2.imdecode(dados, cv2.IMREAD_COLOR)
    except Exception:
        return cv2.imread(caminho_imagem)


def normalizar_gabarito_oficial(gabarito: Dict[str, str]) -> Dict[str, str]:
    """Garante chaves 1..N em texto e respostas em A-E ou NULA."""
    normalizado = {}
    for chave, valor in gabarito.items():
        try:
            q = str(int(chave))
        except Exception:
            q = str(chave).strip()

        alt = str(valor).strip().upper()
        if alt not in ALTERNATIVAS:
            alt = "NULA"
        normalizado[q] = alt

    return dict(sorted(normalizado.items(), key=lambda item: int(item[0])))


def _normalizar_payload_qr(payload: str) -> Optional[Dict[str, Any]]:
    """
    Normaliza QR antigo e QR compacto.
    Nunca devolve chaves importantes como None.

    Formatos aceitos:
    - Antigo: {"aluno_id": 70, "prova_id": 1, ...}
    - Novo compacto: {"v": 3, "s": "atlas", "a": 70, "p": 1, "m": "234567", ...}
    """
    if not payload:
        return None

    try:
        dados = json.loads(str(payload).strip())
    except Exception:
        return {"conteudo_qr": str(payload or "")}

    if not isinstance(dados, dict):
        return {"conteudo_qr": str(payload or "")}

    normalizado = dict(dados)

    # Compatibilidade com QR compacto.
    aliases = {
        "a": "aluno_id",
        "aid": "aluno_id",
        "aluno": "aluno_id",
        "p": "prova_id",
        "pid": "prova_id",
        "prova": "prova_id",
        "m": "matricula",
        "mat": "matricula",
        "t": "turma",
        "sig": "assinatura",
    }
    for curto, completo in aliases.items():
        if completo not in normalizado and curto in normalizado:
            normalizado[completo] = normalizado.get(curto)

    # Garante strings/inteiros limpos onde importa.
    for chave in ["aluno_id", "prova_id", "matricula", "nome", "turma", "titulo", "disciplina", "assinatura"]:
        if normalizado.get(chave) is None:
            normalizado[chave] = ""

    try:
        if normalizado.get("aluno_id") != "":
            normalizado["aluno_id"] = int(normalizado["aluno_id"])
    except Exception:
        normalizado["aluno_id"] = ""

    try:
        if normalizado.get("prova_id") != "":
            normalizado["prova_id"] = int(normalizado["prova_id"])
    except Exception:
        normalizado["prova_id"] = ""

    return normalizado


def _regioes_provaveis_qr(imagem: np.ndarray) -> List[Tuple[str, np.ndarray]]:
    """Recortes prováveis do QR. No Atlas ele fica no cabeçalho superior direito."""
    h, w = imagem.shape[:2]
    regioes = [
        # Os crops vêm primeiro porque são muito mais rápidos e evitam o QR ficar pequeno demais.
        ("cabecalho_qr", imagem[int(h * 0.04):int(h * 0.36), int(w * 0.50):int(w * 0.98)]),
        ("topo_direito", imagem[0:int(h * 0.45), int(w * 0.42):w]),
        ("topo", imagem[0:int(h * 0.45), 0:w]),
        ("inteira", imagem),
    ]
    return [(nome, crop) for nome, crop in regioes if crop is not None and crop.size > 0]


def _rotacionar_imagem(imagem: np.ndarray, angulo: float) -> np.ndarray:
    h, w = imagem.shape[:2]
    matriz = cv2.getRotationMatrix2D((w / 2, h / 2), angulo, 1.0)
    borda = 255 if len(imagem.shape) == 2 else (255, 255, 255)
    return cv2.warpAffine(
        imagem,
        matriz,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=borda,
    )


def _variantes_qr(imagem: np.ndarray) -> List[Tuple[str, np.ndarray]]:
    """Variações de pré-processamento para QR com sombra, ruído ou baixo contraste."""
    if len(imagem.shape) == 2:
        gray = imagem
        bgr = cv2.cvtColor(imagem, cv2.COLOR_GRAY2BGR)
    else:
        bgr = imagem
        gray = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY)

    variantes: List[Tuple[str, np.ndarray]] = [("original", bgr), ("gray", gray)]

    try:
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
        variantes.append(("clahe", clahe))
    except Exception:
        clahe = gray

    variantes.append(("equalizada", cv2.equalizeHist(gray)))

    blur = cv2.GaussianBlur(clahe, (3, 3), 0)
    for bloco in [21, 31, 41]:
        try:
            adaptive = cv2.adaptiveThreshold(
                blur,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                bloco,
                5,
            )
            variantes.append((f"adaptive_{bloco}", adaptive))
        except Exception:
            pass

    return variantes


def _decodificar_qr_opencv(imagem: np.ndarray) -> str:
    detector = cv2.QRCodeDetector()

    try:
        dados, _, _ = detector.detectAndDecode(imagem)
        if dados:
            return str(dados)
    except Exception:
        pass

    try:
        ok, lista, _, _ = detector.detectAndDecodeMulti(imagem)
        if ok and lista:
            for item in lista:
                if item:
                    return str(item)
    except Exception:
        pass

    return ""


def ler_qrcode(caminho_imagem: str) -> Optional[Dict[str, Any]]:
    """
    Leitor resiliente de QR Code.

    Pipeline:
    1. leitura direta;
    2. recortes do cabeçalho/topo direito;
    3. aumento de escala;
    4. CLAHE/equalização;
    5. threshold adaptativo;
    6. rotações pequenas.

    Se falhar, retorna None. O app.py deve tratar None como pendência manual,
    nunca como autorização para salvar em aluno/prova base.
    """
    imagem = ler_imagem(caminho_imagem)
    if imagem is None or imagem.size == 0:
        return None

    # Tentativa direta, mais rápida.
    payload = _decodificar_qr_opencv(imagem)
    if payload:
        return _normalizar_payload_qr(payload)

    escalas_padrao = [1.0, 1.5, 2.0, 3.0]
    angulos_padrao = [0, -3, 3, -6, 6, -10, 10]

    for nome_regiao, regiao in _regioes_provaveis_qr(imagem):
        for escala in escalas_padrao:
            # Não aumenta a imagem inteira: fica lento demais no Scan.
            if nome_regiao == "inteira" and escala != 1.0:
                continue

            # Evita explosão de processamento em crops muito grandes.
            if regiao.shape[0] * regiao.shape[1] > 1_800_000 and escala > 1.5:
                continue

            if escala == 1.0:
                redimensionada = regiao
            else:
                redimensionada = cv2.resize(regiao, None, fx=escala, fy=escala, interpolation=cv2.INTER_CUBIC)

            angulos = [0] if nome_regiao in ["inteira", "topo"] else angulos_padrao
            for angulo in angulos:
                ajustada = _rotacionar_imagem(redimensionada, angulo) if angulo else redimensionada
                for _, variante in _variantes_qr(ajustada):
                    payload = _decodificar_qr_opencv(variante)
                    if payload:
                        return _normalizar_payload_qr(payload)

    return None


def preparar_imagem(caminho_imagem: str):
    imagem = ler_imagem(caminho_imagem)

    if imagem is None:
        return None, None, None

    gray = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)

    thresh = cv2.threshold(
        blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )[1]

    return imagem, gray, thresh


def _contornos_candidatos_bolhas(thresh: np.ndarray) -> List[Dict[str, Any]]:
    """
    Encontra círculos candidatos a bolhas.
    Funciona para a folha gerada em templates/folha.html.
    """
    altura, largura = thresh.shape[:2]
    area_img = altura * largura

    contornos, _ = cv2.findContours(
        thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    candidatos = []
    for contorno in contornos:
        x, y, w, h = cv2.boundingRect(contorno)
        area = cv2.contourArea(contorno)

        if w <= 0 or h <= 0:
            continue

        # Tamanho dinâmico para aceitar print, scanner ou foto.
        min_lado = max(8, int(min(largura, altura) * 0.006))
        max_lado = max(26, int(min(largura, altura) * 0.045))

        if w < min_lado or h < min_lado or w > max_lado or h > max_lado:
            continue

        proporcao = w / float(h)
        if proporcao < 0.65 or proporcao > 1.35:
            continue

        if area < 25 or area > area_img * 0.005:
            continue

        perimetro = cv2.arcLength(contorno, True)
        if perimetro == 0:
            continue

        circularidade = 4 * np.pi * area / (perimetro * perimetro)
        if circularidade < 0.35:
            continue

        # Evita pegar QR Code e textos do cabeçalho.
        # As bolhas ficam depois do cabeçalho da folha.
        if y < altura * 0.18:
            continue

        candidatos.append(
            {
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "cx": x + w / 2,
                "cy": y + h / 2,
                "area": area,
                "circularidade": circularidade,
            }
        )

    # Filtro final por tamanho real das bolhas.
    # Sem isso, os números da questão (ex.: 07 e 10) podem virar
    # "bolhas falsas". Foi isso que fazia uma prova 100% correta cair
    # para 80%: as questões 7 e 10 eram montadas com pedaços do número
    # da questão no lugar das alternativas A/B/C/D/E.
    if len(candidatos) >= 5:
        larguras = np.array([c["w"] for c in candidatos], dtype=float)
        alturas = np.array([c["h"] for c in candidatos], dtype=float)
        areas = np.array([c["area"] for c in candidatos], dtype=float)

        med_w = float(np.median(larguras))
        med_h = float(np.median(alturas))
        med_area = float(np.median(areas))

        min_w_final = max(8.0, med_w * 0.65)
        min_h_final = max(8.0, med_h * 0.65)
        min_area_final = max(25.0, med_area * 0.45)
        max_w_final = med_w * 1.55
        max_h_final = med_h * 1.55
        max_area_final = med_area * 1.80

        candidatos_filtrados = []
        for c in candidatos:
            if (
                c["w"] >= min_w_final
                and c["h"] >= min_h_final
                and c["area"] >= min_area_final
                and c["w"] <= max_w_final
                and c["h"] <= max_h_final
                and c["area"] <= max_area_final
            ):
                candidatos_filtrados.append(c)

        # Só troca se o filtro ainda deixou bolhas suficientes.
        # Isso evita quebrar fotos muito ruins ou folhas muito pequenas.
        if len(candidatos_filtrados) >= 5:
            candidatos = candidatos_filtrados

    return candidatos


def _agrupar_por_linhas(candidatos: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    if not candidatos:
        return []

    alturas = [c["h"] for c in candidatos]
    tolerancia = max(8, float(np.median(alturas)) * 0.75)

    linhas = []
    for c in sorted(candidatos, key=lambda item: item["cy"]):
        colocado = False
        for linha in linhas:
            media_y = float(np.mean([item["cy"] for item in linha]))
            if abs(c["cy"] - media_y) <= tolerancia:
                linha.append(c)
                colocado = True
                break
        if not colocado:
            linhas.append([c])

    linhas = [sorted(linha, key=lambda item: item["cx"]) for linha in linhas]
    return linhas


def _separar_grupos_de_questoes(linhas: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Cada questão precisa ter 5 bolhas na mesma linha: A, B, C, D e E.
    Como a folha tem várias colunas, uma mesma linha visual pode conter
    as questões 1, 11, 21, 31...
    """
    grupos = []

    for linha in linhas:
        if len(linha) < 5:
            continue

        xs = [item["cx"] for item in linha]
        gaps = np.diff(xs) if len(xs) > 1 else []

        if len(gaps) == 0:
            continue

        # Gaps pequenos são entre A-B-C-D-E. Gaps grandes são entre colunas.
        gaps_ordenados = sorted(float(g) for g in gaps if g > 0)
        if not gaps_ordenados:
            continue

        gap_base = np.median(gaps_ordenados[: min(10, len(gaps_ordenados))])
        limite_quebra = max(gap_base * 2.2, gap_base + 18)

        bloco = [linha[0]]
        blocos = []
        for anterior, atual, gap in zip(linha, linha[1:], gaps):
            if gap > limite_quebra:
                blocos.append(bloco)
                bloco = [atual]
            else:
                bloco.append(atual)
        blocos.append(bloco)

        # Se algum bloco ficou grande demais, corta em grupos de 5.
        blocos_finais = []
        for bloco in blocos:
            if len(bloco) == 5:
                blocos_finais.append(bloco)
            elif len(bloco) > 5:
                for i in range(0, len(bloco), 5):
                    pedaco = bloco[i : i + 5]
                    if len(pedaco) == 5:
                        blocos_finais.append(pedaco)

        for bloco in blocos_finais:
            bloco = sorted(bloco, key=lambda item: item["cx"])
            grupos.append(
                {
                    "bolhas": bloco,
                    "cx": float(np.mean([b["cx"] for b in bloco])),
                    "cy": float(np.mean([b["cy"] for b in bloco])),
                }
            )

    return grupos


def _mapear_grupos_para_questoes(
    grupos: List[Dict[str, Any]], total_questoes: int
) -> Dict[str, List[Dict[str, Any]]]:
    """Ordena as questões por coluna e linha conforme a folha HTML."""
    if not grupos:
        return {}

    # Agrupa colunas pelos centros X dos blocos de questões.
    grupos_ordenados_x = sorted(grupos, key=lambda g: g["cx"])
    larguras = []
    for g in grupos:
        bolhas = g["bolhas"]
        larguras.append(max(b["cx"] for b in bolhas) - min(b["cx"] for b in bolhas))

    largura_grupo = float(np.median(larguras)) if larguras else 80.0
    tolerancia_coluna = max(40.0, largura_grupo * 1.4)

    colunas = []
    for grupo in grupos_ordenados_x:
        colocado = False
        for coluna in colunas:
            media_x = float(np.mean([g["cx"] for g in coluna]))
            if abs(grupo["cx"] - media_x) <= tolerancia_coluna:
                coluna.append(grupo)
                colocado = True
                break
        if not colocado:
            colunas.append([grupo])

    colunas = [sorted(coluna, key=lambda g: g["cy"]) for coluna in colunas]
    colunas = sorted(colunas, key=lambda coluna: np.mean([g["cx"] for g in coluna]))

    mapa = {}
    for indice_coluna, coluna in enumerate(colunas):
        for indice_linha, grupo in enumerate(coluna):
            questao = indice_coluna * QUESTOES_POR_COLUNA + indice_linha + 1
            if questao <= total_questoes:
                mapa[str(questao)] = grupo["bolhas"]

    return mapa


def _pontuacao_preenchimento(thresh: np.ndarray, bolha: Dict[str, Any]) -> float:
    """Calcula quanto do interior da bolha está escuro/preenchido."""
    x, y, w, h = int(bolha["x"]), int(bolha["y"]), int(bolha["w"]), int(bolha["h"])
    recorte = thresh[y : y + h, x : x + w]

    if recorte.size == 0:
        return 0.0

    yy, xx = np.ogrid[:h, :w]
    cx = w / 2
    cy = h / 2
    raio = min(w, h) * 0.34

    # Usa só a região mais interna para ignorar a borda do círculo.
    mascara = (xx - cx) ** 2 + (yy - cy) ** 2 <= raio**2
    if not np.any(mascara):
        return 0.0

    pixels = recorte[mascara]
    return float(np.count_nonzero(pixels > 0) / pixels.size)


def _pontuacao_circulo(gray: np.ndarray, circulo: Dict[str, Any]) -> float:
    """
    Pontuação baseada no interior real do círculo detectado por Hough.
    Ajuda em folhas antigas onde as letras A/B/C/D/E estavam dentro das bolhas.
    """
    x, y, r = int(circulo["cx"]), int(circulo["cy"]), int(circulo["r"])
    r = max(4, r)

    y1, y2 = max(0, y - r), min(gray.shape[0], y + r + 1)
    x1, x2 = max(0, x - r), min(gray.shape[1], x + r + 1)
    recorte = gray[y1:y2, x1:x2]
    if recorte.size == 0:
        return 0.0

    blur = cv2.GaussianBlur(recorte, (3, 3), 0)
    th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]

    h, w = th.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    cx = w / 2
    cy = h / 2

    # Usa o interior do círculo. Em folha nova as bolhas ficam vazias; em folha antiga
    # pode haver letra no centro, mas uma bolha preenchida ainda fica muito mais escura.
    raio_interno = min(w, h) * 0.33
    mascara = (xx - cx) ** 2 + (yy - cy) ** 2 <= raio_interno ** 2
    if not np.any(mascara):
        return 0.0

    return float(np.count_nonzero(th[mascara] > 0) / np.count_nonzero(mascara))


def _mesclar_circulos(circulos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove círculos duplicados vindos do HoughCircles."""
    saida: List[Dict[str, Any]] = []
    for c in sorted(circulos, key=lambda item: (item["cy"], item["cx"], -item["r"])):
        duplicado = False
        for s in saida:
            dist = ((c["cx"] - s["cx"]) ** 2 + (c["cy"] - s["cy"]) ** 2) ** 0.5
            if dist < max(8, min(c["r"], s["r"]) * 0.75):
                # Mantém o círculo maior/mais estável.
                if c["r"] > s["r"]:
                    s.update(c)
                duplicado = True
                break
        if not duplicado:
            saida.append(c)
    return saida


def _detectar_circulos_hough(gray: np.ndarray) -> List[Dict[str, Any]]:
    """Detecta círculos de alternativas por HoughCircles."""
    altura, largura = gray.shape[:2]
    min_dim = min(altura, largura)
    min_r = max(6, int(min_dim * 0.006))
    max_r = max(12, int(min_dim * 0.026))

    blur = cv2.medianBlur(gray, 5)
    encontrados: List[Dict[str, Any]] = []

    # Mais sensível primeiro, depois mais rígido.
    for param2 in [40, 35, 30, 25, 22]:
        circles = cv2.HoughCircles(
            blur,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=max(18, int(min_dim * 0.018)),
            param1=100,
            param2=param2,
            minRadius=min_r,
            maxRadius=max_r,
        )
        if circles is None:
            continue
        for x, y, r in np.round(circles[0, :]).astype(int):
            # Ignora topo muito alto e rodapé. As respostas ficam abaixo do cabeçalho.
            if y < altura * 0.16 or y > altura * 0.92:
                continue
            # Evita a área do QR no topo direito.
            if y < altura * 0.32 and x > largura * 0.55:
                continue
            encontrados.append({"cx": float(x), "cy": float(y), "r": float(r), "x": int(x-r), "y": int(y-r), "w": int(2*r), "h": int(2*r)})

        encontrados = _mesclar_circulos(encontrados)
        if len(encontrados) >= 5:
            break

    return _mesclar_circulos(encontrados)


def _separar_grupos_circulos(circulos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Agrupa círculos em blocos de 5 alternativas A-E."""
    if not circulos:
        return []

    raios = [c["r"] for c in circulos]
    tolerancia_y = max(10.0, float(np.median(raios)) * 1.25)

    linhas: List[List[Dict[str, Any]]] = []
    for c in sorted(circulos, key=lambda item: item["cy"]):
        colocado = False
        for linha in linhas:
            media_y = float(np.mean([item["cy"] for item in linha]))
            if abs(c["cy"] - media_y) <= tolerancia_y:
                linha.append(c)
                colocado = True
                break
        if not colocado:
            linhas.append([c])

    grupos: List[Dict[str, Any]] = []
    for linha in linhas:
        linha = sorted(linha, key=lambda item: item["cx"])
        if len(linha) < 5:
            continue

        # Divide por gaps grandes. Depois, se ainda sobrar bloco grande, tenta janelas de 5.
        gaps = [linha[i + 1]["cx"] - linha[i]["cx"] for i in range(len(linha) - 1)]
        positivos = [g for g in gaps if g > 0]
        gap_base = float(np.median(positivos)) if positivos else 0.0
        limite = max(36.0, gap_base * 1.9) if gap_base else 90.0

        blocos = []
        bloco = [linha[0]]
        for anterior, atual, gap in zip(linha, linha[1:], gaps):
            if gap > limite:
                blocos.append(bloco)
                bloco = [atual]
            else:
                bloco.append(atual)
        blocos.append(bloco)

        for bloco in blocos:
            if len(bloco) == 5:
                grupos.append({"bolhas": bloco, "cx": float(np.mean([b["cx"] for b in bloco])), "cy": float(np.mean([b["cy"] for b in bloco]))})
            elif len(bloco) > 5:
                # Procura sequência de 5 com espaçamento mais regular.
                melhor = None
                melhor_var = None
                for i in range(0, len(bloco) - 4):
                    pedaco = bloco[i:i+5]
                    gs = [pedaco[j+1]["cx"] - pedaco[j]["cx"] for j in range(4)]
                    media = np.mean(gs)
                    if media <= 0:
                        continue
                    var = float(np.std(gs) / media)
                    if melhor is None or var < melhor_var:
                        melhor = pedaco
                        melhor_var = var
                if melhor is not None and melhor_var is not None and melhor_var < 0.35:
                    grupos.append({"bolhas": melhor, "cx": float(np.mean([b["cx"] for b in melhor])), "cy": float(np.mean([b["cy"] for b in melhor]))})

    return grupos


def detectar_respostas_hough(caminho_imagem: str, total_questoes: int) -> Dict[str, Any]:
    """Fallback OMR por HoughCircles para evitar resposta NULA falsa."""
    imagem = ler_imagem(caminho_imagem)
    if imagem is None:
        return {"status_omr": "erro", "mensagem": "Imagem não pôde ser lida.", "respostas": {}}

    gray = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY)
    circulos = _detectar_circulos_hough(gray)
    grupos = _separar_grupos_circulos(circulos)
    mapa_questoes = _mapear_grupos_para_questoes(grupos, total_questoes)

    respostas = {str(i): "NULA" for i in range(1, total_questoes + 1)}
    confiancas = {str(i): "baixa" for i in range(1, total_questoes + 1)}
    scores_por_questao = {}

    for questao, bolhas in mapa_questoes.items():
        if len(bolhas) != 5:
            continue
        scores = {}
        for alt, bolha in zip(ALTERNATIVAS, bolhas):
            scores[alt] = round(_pontuacao_circulo(gray, bolha), 4)
        resposta, confianca = _decidir_alternativa_hough(scores)
        respostas[questao] = resposta
        confiancas[questao] = confianca
        scores_por_questao[questao] = scores

    detectadas = sum(1 for r in respostas.values() if r != "NULA")
    status = "ok" if detectadas > 0 else "sem_marcacoes_detectadas"
    return {
        "status_omr": status,
        "mensagem": "OMR executado por fallback HoughCircles." if detectadas else "Fallback HoughCircles não encontrou marcação clara.",
        "respostas": respostas,
        "confiancas": confiancas,
        "questoes_mapeadas": len(mapa_questoes),
        "bolhas_candidatas": len(circulos),
        "marcacoes_detectadas": detectadas,
        "scores": scores_por_questao,
        "metodo": "hough",
    }


def _decidir_alternativa_hough(scores: Dict[str, float]) -> Tuple[str, str]:
    """Decisão específica para círculos reais detectados por Hough."""
    if not scores:
        return "NULA", "baixa"

    ordenados = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    melhor_alt, melhor_score = ordenados[0]
    segundo_score = ordenados[1][1] if len(ordenados) > 1 else 0.0
    diferenca = melhor_score - segundo_score

    # Bolha totalmente preenchida, comum na folha do Atlas.
    if melhor_score >= 0.82 and diferenca >= 0.12:
        return melhor_alt, "alta"

    # Marca mais leve, mas isolada das demais.
    if melhor_score >= 0.55 and diferenca >= 0.18:
        return melhor_alt, "media"

    # Para folhas novas sem letra dentro da bolha, os vazios ficam próximos de zero.
    if melhor_score >= 0.35 and diferenca >= 0.20:
        return melhor_alt, "media"

    return "NULA", "baixa"


def _decidir_alternativa(scores: Dict[str, float]) -> Tuple[str, str]:
    """Decide alternativa marcada ou NULA a partir dos preenchimentos.

    A versão anterior era rígida demais. Em folha com poucas questões ou foto
    mais clara, a marcação podia ficar abaixo do limite e virar NULA.
    Agora a decisão é relativa: compara a melhor bolha com as outras da MESMA
    questão, sem depender de um valor fixo alto.
    """
    if not scores:
        return "NULA", "baixa"

    ordenados = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    melhor_alt, melhor_score = ordenados[0]
    segundo_score = ordenados[1][1] if len(ordenados) > 1 else 0.0

    outros = [valor for alt, valor in scores.items() if alt != melhor_alt]
    media_outros = float(np.mean(outros)) if outros else 0.0
    mediana_outros = float(np.median(outros)) if outros else 0.0
    diferenca = melhor_score - segundo_score

    # Se duas bolhas estão muito fortes e parecidas, considera rasura/dupla marcação.
    bolhas_fortes = [alt for alt, valor in scores.items() if valor >= 0.32]
    if len(bolhas_fortes) >= 2 and segundo_score >= max(0.28, melhor_score * 0.72):
        return "NULA", "baixa"

    # Marca clara: bem acima das demais.
    if melhor_score >= 0.30 and diferenca >= 0.075 and melhor_score >= mediana_outros + 0.10:
        return melhor_alt, "alta"

    # Marca leve: aceita se ainda estiver isolada em relação às outras bolhas.
    if melhor_score >= 0.22 and diferenca >= 0.06 and melhor_score >= media_outros * 1.35 + 0.025:
        return melhor_alt, "media"

    return "NULA", "baixa"

def detectar_respostas_omr(caminho_imagem: str, total_questoes: int) -> Dict[str, Any]:
    imagem, gray, thresh = preparar_imagem(caminho_imagem)

    if imagem is None:
        return {
            "status_omr": "erro",
            "mensagem": "Imagem não pôde ser lida pelo OpenCV.",
            "respostas": {},
        }

    respostas = {str(i): "NULA" for i in range(1, total_questoes + 1)}
    confiancas = {str(i): "baixa" for i in range(1, total_questoes + 1)}
    scores_por_questao = {}

    candidatos = _contornos_candidatos_bolhas(thresh)
    linhas = _agrupar_por_linhas(candidatos)
    grupos = _separar_grupos_de_questoes(linhas)
    mapa_questoes = _mapear_grupos_para_questoes(grupos, total_questoes)

    for questao, bolhas in mapa_questoes.items():
        if len(bolhas) != 5:
            continue

        scores = {}
        for alt, bolha in zip(ALTERNATIVAS, bolhas):
            scores[alt] = round(_pontuacao_preenchimento(thresh, bolha), 4)

        resposta, confianca = _decidir_alternativa(scores)
        respostas[questao] = resposta
        confiancas[questao] = confianca
        scores_por_questao[questao] = scores

    detectadas = sum(1 for r in respostas.values() if r != "NULA")
    questoes_mapeadas = len(mapa_questoes)

    minimo_questoes_para_ok = min(total_questoes, max(1, int(total_questoes * 0.6)))

    if questoes_mapeadas < minimo_questoes_para_ok:
        status = "falha_calibracao"
        mensagem = (
            "O OpenCV não conseguiu mapear a folha inteira. "
            "A IA será usada como apoio se estiver configurada."
        )
    elif detectadas == 0:
        status = "sem_marcacoes_detectadas"
        mensagem = (
            "A folha foi mapeada, mas nenhuma marca forte foi detectada. "
            "Verifique se as bolhas foram preenchidas com caneta/lápis escuro."
        )
    else:
        status = "ok"
        mensagem = "OMR executado com leitura real das bolhas."

    resultado_contorno = {
        "status_omr": status,
        "mensagem": mensagem,
        "respostas": respostas,
        "confiancas": confiancas,
        "questoes_mapeadas": questoes_mapeadas,
        "bolhas_candidatas": len(candidatos),
        "marcacoes_detectadas": detectadas,
        "scores": scores_por_questao,
        "metodo": "contornos",
    }

    # Fallback importante: em folhas antigas, as letras dentro dos círculos
    # podem fazer o método por contornos confundir vazios com marcação.
    # O HoughCircles procura os círculos reais e costuma corrigir esse caso.
    if detectadas == 0 or status in ["falha_calibracao", "sem_marcacoes_detectadas", "erro"]:
        try:
            resultado_hough = detectar_respostas_hough(caminho_imagem, total_questoes)
            if int(resultado_hough.get("marcacoes_detectadas") or 0) > detectadas:
                resultado_hough["fallback_de"] = resultado_contorno
                return resultado_hough
        except Exception as erro:
            resultado_contorno["erro_fallback_hough"] = str(erro)

    return resultado_contorno


def extrair_json(texto: str) -> Dict[str, Any]:
    texto = (texto or "").strip()

    try:
        return json.loads(texto)
    except Exception:
        pass

    texto = texto.replace("```json", "").replace("```", "").strip()

    inicio = texto.find("{")
    fim = texto.rfind("}")

    if inicio >= 0 and fim > inicio:
        return json.loads(texto[inicio : fim + 1])

    raise ValueError("A IA não retornou JSON válido.")


def normalizar_respostas(dados: Dict[str, Any], total_questoes: int) -> Dict[str, str]:
    respostas = {}
    validas = ALTERNATIVAS + ["NULA"]

    for i in range(1, total_questoes + 1):
        q = str(i)
        alt = str(dados.get(q, "NULA")).strip().upper()

        if alt in ["", "-", "BRANCO", "EM BRANCO", "NONE", "NULL"]:
            alt = "NULA"

        if alt not in validas:
            alt = "NULA"

        respostas[q] = alt

    return respostas


def normalizar_confianca(valor) -> str:
    texto = str(valor or "media").strip().lower()

    if texto in ["alta", "alto", "high", "boa", "confiavel", "confiável"]:
        return "alta"

    if texto in ["baixa", "baixo", "low", "ruim"]:
        return "baixa"

    return "media"


def ler_respostas_com_ia(
    caminho_gabarito_aluno: str,
    gabarito_oficial: Dict[str, str],
    observacao_omr: str = "",
):
    if not GOOGLE_API_KEY:
        raise RuntimeError(
            "GOOGLE_API_KEY não encontrada. Confira se o arquivo .env existe."
        )

    if not genai or not types:
        raise RuntimeError(
            "Biblioteca google-genai não foi carregada. Rode: pip install -r requirements.txt"
        )

    if not gemini_client:
        raise RuntimeError("Cliente Gemini não foi inicializado.")

    total = len(gabarito_oficial)

    prompt = f"""
Você é um módulo de apoio visual para um corretor de provas OMR.

Leia a folha de respostas do aluno na imagem enviada.

REGRAS OBRIGATÓRIAS:
- Retorne SOMENTE JSON válido.
- Use apenas A, B, C, D, E ou NULA.
- Analise a folha inteira, questão por questão.
- Cada questão tem bolhas/círculos A, B, C, D e E.
- A alternativa marcada normalmente está preenchida/escurecida.
- Se nenhuma alternativa estiver claramente preenchida, retorne NULA.
- Se houver rasura, dúvida ou duas alternativas marcadas na mesma questão, retorne NULA.
- Não use o gabarito oficial para chutar resposta.
- O total de questões é {total}.
- Devolva todas as questões de 1 até {total}.

Observação do OMR local:
{observacao_omr}

Formato obrigatório:
{{
  "gabarito_aluno_extraido": {{
    "1": "A",
    "2": "NULA",
    "3": "C"
  }},
  "questoes_duvidosas": [],
  "confianca_geral": "alta"
}}
"""

    with open(caminho_gabarito_aluno, "rb") as f:
        imagem_bytes = f.read()

    nome = caminho_gabarito_aluno.lower()
    mime_type = "image/jpeg" if nome.endswith((".jpg", ".jpeg")) else "image/png"

    resposta = gemini_client.models.generate_content(
        model=MODELO_GEMINI,
        contents=[
            prompt,
            types.Part.from_bytes(data=imagem_bytes, mime_type=mime_type),
        ],
        config={"temperature": 0, "response_mime_type": "application/json"},
    )

    dados = extrair_json(resposta.text)

    respostas = normalizar_respostas(
        dados.get("gabarito_aluno_extraido", {}), total
    )

    return {
        "respostas": respostas,
        "questoes_duvidosas": dados.get("questoes_duvidosas", []),
        "confianca_geral": normalizar_confianca(dados.get("confianca_geral", "media")),
        "modelo": MODELO_GEMINI,
    }


def escolher_respostas_finais(respostas_omr, respostas_ia, total_questoes):
    """
    Junta OMR + IA sem deixar uma leitura vazia apagar a outra.
    No código antigo, o OMR retornava tudo NULA e a nota acabava 0%.
    """
    finais = {}
    ia = respostas_ia.get("respostas", {}) if respostas_ia else {}
    conf_ia = normalizar_confianca(
        respostas_ia.get("confianca_geral", "media") if respostas_ia else "baixa"
    )
    duvidosas = {
        str(q) for q in (respostas_ia.get("questoes_duvidosas", []) if respostas_ia else [])
    }

    confiancas_omr = respostas_omr.get("confiancas", {}) if isinstance(respostas_omr, dict) else {}
    respostas_omr_dict = respostas_omr.get("respostas", respostas_omr) if isinstance(respostas_omr, dict) else {}

    for i in range(1, total_questoes + 1):
        q = str(i)
        r_omr = str(respostas_omr_dict.get(q, "NULA")).upper()
        r_ia = str(ia.get(q, "NULA")).upper()
        conf_omr = normalizar_confianca(confiancas_omr.get(q, "baixa"))

        if q in duvidosas:
            finais[q] = "NULA"
        elif r_omr in ALTERNATIVAS and r_ia in ALTERNATIVAS:
            if r_omr == r_ia:
                finais[q] = r_omr
            elif conf_omr == "alta" and conf_ia != "alta":
                finais[q] = r_omr
            elif conf_ia == "alta" and conf_omr != "alta":
                finais[q] = r_ia
            else:
                finais[q] = "NULA"
        elif r_omr in ALTERNATIVAS:
            finais[q] = r_omr
        elif r_ia in ALTERNATIVAS:
            finais[q] = r_ia
        else:
            finais[q] = "NULA"

    return finais


def comparar_respostas(gabarito_oficial, respostas_aluno):
    gabarito_oficial = normalizar_gabarito_oficial(gabarito_oficial)

    acertos = 0
    erros = 0
    anuladas = 0
    detalhes = []

    questoes = sorted(gabarito_oficial.keys(), key=lambda x: int(x))

    for questao in questoes:
        correta = str(gabarito_oficial.get(questao, "NULA")).upper()
        marcada = str(respostas_aluno.get(questao, "NULA")).upper()

        if marcada == "NULA":
            status = "anulada_ou_em_branco"
            anuladas += 1
        elif marcada == correta:
            status = "correta"
            acertos += 1
        else:
            status = "errada"
            erros += 1

        detalhes.append(
            {
                "questao": questao,
                "resposta_correta": correta,
                "resposta_aluno": marcada,
                "status": status,
            }
        )

    total = len(questoes)
    nota = round((acertos / total) * 100, 2) if total else 0

    return {
        "total_questoes_oficial": total,
        "acertos": acertos,
        "erros": erros,
        "anuladas_ou_em_branco": anuladas,
        "nota_percentual": nota,
        "detalhes": detalhes,
    }


def corrigir_imagem_web(
    caminho_gabarito_aluno: str,
    gabarito_oficial: Dict[str, str],
    caminho_gabarito_professor: Optional[str] = None,
    usar_ia="auto",
):
    gabarito_oficial = normalizar_gabarito_oficial(gabarito_oficial)
    total = len(gabarito_oficial)
    dados_qr = ler_qrcode(caminho_gabarito_aluno)

    omr = detectar_respostas_omr(caminho_gabarito_aluno, total)

    ia_resultado = None
    erro_ia = None

    status_omr = str(omr.get("status_omr", "")).lower()
    questoes_mapeadas = int(omr.get("questoes_mapeadas") or 0)
    marcacoes_detectadas = int(omr.get("marcacoes_detectadas") or 0)
    precisa_ia = (
        status_omr in {"falha_calibracao", "sem_marcacoes_detectadas", "erro"}
        or questoes_mapeadas < max(1, int(total * 0.80))
        or marcacoes_detectadas == 0
    )

    # Política ATLAS: IA mínima.
    # usar_ia=True força IA; usar_ia=False desliga; usar_ia="auto" chama IA só quando o OpenCV fica pouco confiável.
    deve_usar_ia = bool(usar_ia is True or (str(usar_ia).lower() == "auto" and precisa_ia))

    if deve_usar_ia:
        try:
            ia_resultado = ler_respostas_com_ia(
                caminho_gabarito_aluno=caminho_gabarito_aluno,
                gabarito_oficial=gabarito_oficial,
                observacao_omr=omr.get("mensagem", ""),
            )
        except Exception as erro:
            erro_ia = str(erro)
    else:
        erro_ia = "IA não usada: leitura por OpenCV suficiente ou modo em tempo real."

    respostas_finais = escolher_respostas_finais(omr, ia_resultado, total)
    resultado = comparar_respostas(gabarito_oficial, respostas_finais)

    # Debug aparece no terminal do Flask. Isso ajuda a descobrir por que veio 0%.
    print("\n===== DEBUG CORREÇÃO =====")
    print("QR:", dados_qr)
    print("Status OMR:", omr.get("status_omr"), "|", omr.get("mensagem"))
    print("Questões mapeadas:", omr.get("questoes_mapeadas"))
    print("Marcações OMR:", omr.get("marcacoes_detectadas"))
    print("Erro IA:", erro_ia)
    print("Respostas finais:", respostas_finais)
    print("Resultado:", resultado)
    print("==========================\n")

    return {
        "status": "corrigido_com_python_opencv_ia",
        "dados_qr": dados_qr,
        "gabarito_professor_extraido": gabarito_oficial,
        "gabarito_aluno_extraido": respostas_finais,
        "resultado": resultado,
        "processamento": {
            "omr": omr,
            "ia": ia_resultado,
            "ia_usada": bool(ia_resultado),
            "erro_ia": erro_ia,
            "modelo_ia": MODELO_GEMINI if GOOGLE_API_KEY else None,
        },
    }
